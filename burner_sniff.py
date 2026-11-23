#!/usr/bin/env python3
"""Pellet burner module-port (J4) listen-only decoder.  NEVER transmits.

  py burner_sniff.py --port COM8 --duration 60
  py burner_sniff.py --hex "7e 01 01 00 01 d5 65 7e"
"""
import argparse, json, struct, sys, time, datetime, collections

FLAG, ESC = 0x7E, 0x7D
TYPES = {0: "none", 1: "u32", 2: "i32", 3: "float", 4: "bytes"}


# ---------- protocol helpers (shared with burner_responder.py) ----------
def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def unescape(raw: bytes):
    """Returns bytes, or None on a bad escape sequence."""
    out, i = bytearray(), 0
    while i < len(raw):
        b = raw[i]
        if b == ESC:
            if i + 1 >= len(raw):
                return None
            n = raw[i + 1]
            if n == 0x5E:
                out.append(0x7E)
            elif n == 0x5D:
                out.append(0x7D)
            else:
                return None
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0x7E:
            out += b"\x7d\x5e"
        elif b == 0x7D:
            out += b"\x7d\x5d"
        else:
            out.append(b)
    return bytes(out)


def build_frame(hdr1, reg, flags, typ, payload=b"") -> bytes:
    body = bytes([hdr1, reg, flags, typ]) + payload
    c = crc16(body)
    return bytes([FLAG]) + escape(body + struct.pack(">H", c)) + bytes([FLAG])


class Frame:
    def __init__(self, raw, body, crc_ok, err=None):
        self.raw, self.body, self.crc_ok, self.err = raw, body, crc_ok, err
        if body and len(body) >= 4:
            self.hdr1, self.reg, self.flags, self.typ = body[:4]
            self.payload = body[4:]
        else:
            self.hdr1 = self.reg = self.flags = self.typ = None
            self.payload = b""

    @property
    def write(self): return bool(self.flags & 0x20)
    @property
    def error(self): return bool(self.flags & 0x10)

    def key(self):
        if self.hdr1 is None:
            return "malformed"
        kind = "write" if self.write else "read"
        if self.error: kind += "+ERR"
        return f"hdr1=0x{self.hdr1:02x} reg{self.reg} {kind} {TYPES.get(self.typ, self.typ)}"

    def decode_payload(self):
        p, t = self.payload, self.typ
        if not p:
            return "(no payload)"
        if t in (1, 2, 3) and len(p) == 4:
            le = struct.unpack("<" + {1: "I", 2: "i", 3: "f"}[t], p)[0]
            be = struct.unpack(">" + {1: "I", 2: "i", 3: "f"}[t], p)[0]
            return f"LE={le}  BE={be}"
        if t == 4 or p[:1] in (b"{", b"["):
            txt = p.rstrip(b"\x04").decode("utf-8", "replace")
            eot = " [+EOT 0x04]" if p.endswith(b"\x04") else ""
            return f"text: {txt}{eot}"
        return "hex: " + p.hex(" ")

    def describe(self):
        if self.hdr1 is None:
            return f"MALFORMED ({self.err}) raw={self.raw.hex(' ')}"
        crc = "CRC ok" if self.crc_ok else "CRC BAD"
        return (f"{crc} hdr1=0x{self.hdr1:02x} reg={self.reg} flags=0x{self.flags:02x}"
                f"({'W' if self.write else 'R'}{',ERR' if self.error else ''}) "
                f"type={TYPES.get(self.typ, self.typ)} len={len(self.payload)} "
                f"{self.decode_payload()}")


def parse_inner(raw_between: bytes, raw_full: bytes) -> Frame:
    body = unescape(raw_between)
    if body is None:
        return Frame(raw_full, None, False, "bad escape")
    if len(body) < 6:
        return Frame(raw_full, None, False, "too short")
    data, rx = body[:-2], struct.unpack(">H", body[-2:])[0]
    return Frame(raw_full, data, crc16(data) == rx)


class Splitter:
    """Feeds on bytes, yields Frames between 7E delimiters."""
    def __init__(self):
        self.buf, self.inframe = bytearray(), False

    def feed(self, data: bytes):
        for b in data:
            if b == FLAG:
                if self.inframe and self.buf:
                    raw = bytes(self.buf)
                    yield parse_inner(raw, bytes([FLAG]) + raw + bytes([FLAG]))
                    self.buf.clear()
                    self.inframe = False   # closing flag consumed; next 7E opens
                else:
                    self.buf.clear()
                    self.inframe = True
            elif self.inframe:
                self.buf.append(b)


# ---------- CLI ----------
class Logger:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        print(msg, flush=True)
        if self.f:
            self.f.write(msg + "\n"); self.f.flush()


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def summarize(log, frames, crc_err, malformed, dur):
    log(f"\n===== SUMMARY ({dur:.1f} s) =====")
    log(f"frames total: {len(frames)}   CRC errors: {crc_err}   malformed: {malformed}")
    groups = collections.OrderedDict()
    for t, f in frames:
        groups.setdefault(f.key() + ("" if f.crc_ok else " [CRC BAD]"), []).append(t)
    for k, ts_ in groups.items():
        iv = [b - a for a, b in zip(ts_, ts_[1:])]
        if iv:
            log(f"  {k:42s} n={len(ts_):4d}  interval avg={sum(iv)/len(iv):.3f}s "
                f"min={min(iv):.3f}s max={max(iv):.3f}s")
        else:
            log(f"  {k:42s} n={len(ts_):4d}  interval n/a")


def main():
    ap = argparse.ArgumentParser(description="Listen-only Pellet burner RS485 decoder")
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--baud", type=int, default=57600)
    ap.add_argument("--duration", type=float, default=0, help="seconds, 0 = until Ctrl+C")
    ap.add_argument("--log", default="sniff.log")
    ap.add_argument("--hex", help="decode a hex string instead of reading the port")
    a = ap.parse_args()
    log = Logger(a.log)

    if a.hex:
        sp = Splitter()
        for f in sp.feed(bytes.fromhex(a.hex.replace(" ", ""))):
            log(f"{ts()}  {f.describe()}")
        return

    import serial  # pyserial; opened read-only in practice: we never call write()
    s = serial.Serial(a.port, a.baud, bytesize=8, parity="N", stopbits=1, timeout=0.05)
    log(f"# listening on {a.port} @ {a.baud} 8N1 (listen-only)")
    sp, frames, crc_err, malformed = Splitter(), [], 0, 0
    t0 = time.time()
    try:
        while not a.duration or time.time() - t0 < a.duration:
            data = s.read(256)
            if not data:
                continue
            now = time.time()
            for f in sp.feed(data):
                frames.append((now, f))
                if f.hdr1 is None: malformed += 1
                elif not f.crc_ok: crc_err += 1
                log(f"{ts()}  {f.describe()}   | {f.raw.hex(' ')}")
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    summarize(log, frames, crc_err, malformed, time.time() - t0)


if __name__ == "__main__":
    main()
