"""find_port: "auto" picks the one USB-serial adapter, refuses to guess between several."""
import os, sys, glob as _glob
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_responder as br

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))

def with_devices(devs):
    def fake(pat):
        pre = pat.rstrip("*")
        return [d for d in devs if d.startswith(pre)]
    return fake

real_glob = _glob.glob
try:
    _glob.glob = with_devices(["/dev/serial/by-id/usb-FTDI_A-if00-port0", "/dev/ttyUSB0"])
    check("one adapter -> its by-id name", br.find_port("auto"), ("/dev/serial/by-id/usb-FTDI_A-if00-port0", ""))

    _glob.glob = with_devices(["/dev/ttyACM0"])
    check("only ttyACM -> used", br.find_port("auto"), ("/dev/ttyACM0", ""))

    _glob.glob = with_devices([])
    p, note = br.find_port("auto")
    check("none -> no port", p, None)
    check("none -> says so", "no USB-serial" in note, True)

    _glob.glob = with_devices(["/dev/serial/by-id/usb-A", "/dev/serial/by-id/usb-B", "/dev/ttyUSB0", "/dev/ttyUSB1"])
    p, note = br.find_port("auto")
    check("two adapters -> no guess", p, None)
    check("two adapters -> both named", "usb-A" in note and "usb-B" in note, True)

    check("named port used as is", br.find_port("/dev/serial/by-id/usb-B"), ("/dev/serial/by-id/usb-B", ""))
    check("COM port used as is", br.find_port("COM8"), ("COM8", ""))
    check("AUTO in capitals", br.find_port("AUTO")[0], None)
finally:
    _glob.glob = real_glob

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
