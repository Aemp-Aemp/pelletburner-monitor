"""Settings from the environment (.env / docker-compose): every option in .env.example reaches the program."""
import os, re, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import burner_responder as br

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))

NAMES = ["BURNER_PORT", "BURNER_HTTP_PORT", "BURNER_ALLOWED_HOSTS", "BURNER_REMOTE_START", "BURNER_KEEP_DAYS",
         "BURNER_LOG_MAX_MB", "BURNER_MQTT", "BURNER_MQTT_READ_ONLY", "BURNER_MQTT_PREFIX"]
saved = {k: os.environ.get(k) for k in NAMES}

def parse(env, argv=()):
    for k in NAMES:
        os.environ.pop(k, None)
    os.environ.update(env)
    return br.build_parser().parse_args(list(argv))

try:
    a = parse({})
    check("no env: no page", a.serve, 0)
    check("no env: start/stop off", a.allow_remote_start, False)
    check("no env: keep 30 days", a.keep_days, 30.0)
    check("no env: log 20 MB", a.log_max_mb, 20.0)
    check("no env: prefix burner", a.mqtt_prefix, "burner")
    check("no env: mqtt off", a.mqtt, "")

    a = parse({"BURNER_PORT": "auto", "BURNER_HTTP_PORT": "8080", "BURNER_ALLOWED_HOSTS": "lan",
               "BURNER_REMOTE_START": "1", "BURNER_KEEP_DAYS": "90", "BURNER_LOG_MAX_MB": "5",
               "BURNER_MQTT": "ha.local", "BURNER_MQTT_READ_ONLY": "yes", "BURNER_MQTT_PREFIX": "boiler2"})
    check("port", a.port, "auto")
    check("http port", a.serve, 8080)
    check("allowed hosts", a.allowed_hosts, "lan")
    check("start/stop on", a.allow_remote_start, True)
    check("keep days", a.keep_days, 90.0)
    check("log size", a.log_max_mb, 5.0)
    check("mqtt host", a.mqtt, "ha.local")
    check("mqtt read only", a.mqtt_read_only, True)
    check("mqtt prefix", a.mqtt_prefix, "boiler2")

    a = parse({"BURNER_REMOTE_START": "0", "BURNER_HTTP_PORT": "", "BURNER_MQTT_PREFIX": " "})
    check("REMOTE_START=0 -> view only", a.allow_remote_start, False)
    check("empty port -> default", a.serve, 0)
    check("blank prefix -> default", a.mqtt_prefix, "burner")

    a = parse({"BURNER_HTTP_PORT": "8080", "BURNER_KEEP_DAYS": "90"}, ["--serve", "9000", "--keep-days", "7"])
    check("command line wins (port)", a.serve, 9000)
    check("command line wins (days)", a.keep_days, 7.0)

    try:
        parse({"BURNER_KEEP_DAYS": "lots"})
        check("bad number stops", False, True)
    except SystemExit as e:
        check("bad number names the setting", "BURNER_KEEP_DAYS" in str(e), True)

    # every setting in .env.example is passed on by docker-compose.yml, and every BURNER_ name there is known
    example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    compose = open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8").read()
    in_example = set(re.findall(r"^#?(BURNER_\w+)=", example, re.M))
    in_compose = set(re.findall(r"\$\{(BURNER_\w+)", compose))
    check("compose passes every .env.example setting", sorted(in_example - in_compose), [])
    check(".env.example lists every compose setting", sorted(in_compose - in_example), [])
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
