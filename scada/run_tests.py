"""Run every test (scada/_*_test.py), one after the other, and print one line per test and a summary.

    py scada\\run_tests.py              all tests
    py scada\\run_tests.py insights     only tests with "insights" in the name
    py scada\\run_tests.py --clock 23:59   all tests with the wall clock moved to 23:59 (finds time-of-day bugs)
    py scada\\run_tests.py --no-browser    skip the browser parts, even when Playwright is installed (CI)

Exit code 1 if a test fails. SKIP (a test that did not run, e.g. no Playwright in this Python) and XFAIL (a known
bug that is not fixed yet) are shown and counted, but do not fail the run. The browser tests need Playwright:
run with a Python that has it, or they are skipped."""
import glob, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    words = sys.argv[1:]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if "--clock" in words:                                  # --clock 23:59: run with the wall clock moved to that time
        i = words.index("--clock")
        env["PB_FAKE_CLOCK"] = words[i + 1]
        env["PYTHONPATH"] = os.pathsep.join(x for x in (os.path.join(HERE, "_fakeclock"), env.get("PYTHONPATH")) if x)
        words = words[:i] + words[i + 2:]
        print(f"wall clock moved to {env['PB_FAKE_CLOCK']} (Python only; browsers keep the real time)")
    nobrowser = None
    if "--no-browser" in words:                             # a stand-in 'playwright' that fails to import: the tests skip
        words = [w for w in words if w != "--no-browser"]
        nobrowser = tempfile.mkdtemp(prefix="pb_nobrowser_")
        os.makedirs(os.path.join(nobrowser, "playwright"))
        with open(os.path.join(nobrowser, "playwright", "__init__.py"), "w") as f:
            f.write("raise ImportError('browser tests turned off (--no-browser)')\n")
        env["PYTHONPATH"] = os.pathsep.join(x for x in (nobrowser, env.get("PYTHONPATH")) if x)
        print("browser tests turned off (--no-browser)")
    try:
        run(words, env)
    finally:
        if nobrowser:
            shutil.rmtree(nobrowser, ignore_errors=True)


def run(words, env):
    tests = [p for p in sorted(glob.glob(os.path.join(HERE, "_*_test.py")))
             if not words or any(w in os.path.basename(p) for w in words)]
    count = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    notes, t_all = [], time.time()
    for p in tests:
        name, t0 = os.path.basename(p), time.time()
        r = subprocess.run([sys.executable, p], cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = r.stdout.decode("utf-8", "replace").splitlines()
        skip = [x for x in lines if x.startswith("SKIP")]
        res = "FAIL" if r.returncode else "SKIP" if skip else "PASS"
        count[res] += 1
        print(f"{res}  {name:<28} {time.time() - t0:5.0f} s", flush=True)
        if res == "FAIL":
            bad = [x for x in lines if x.startswith("FAIL")] or lines[-15:]
            print("\n".join("      " + x for x in bad[:30]), flush=True)
        notes += [f"{name}: {x}" for x in skip + [x for x in lines if x.startswith("XFAIL")]]
    print(f"\n{len(tests)} tests in {time.time() - t_all:.0f} s: {count['PASS']} passed, {count['FAIL']} failed, "
          f"{count['SKIP']} skipped")
    for n in notes:
        print("  note: " + n)
    sys.exit(1 if count["FAIL"] or not tests else 0)


if __name__ == "__main__":
    main()
