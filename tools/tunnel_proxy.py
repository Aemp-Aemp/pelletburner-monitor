"""Tiny HTTP/HTTPS forward proxy on 127.0.0.1:3128 (this PC only).

The Pi reaches it through an SSH reverse tunnel (ssh -R 3128:127.0.0.1:3128 pelletpi), so the Pi can
use this PC's internet for apt / docker pull without any change to Wi-Fi or Windows network settings.
Stop it by ending the python process.
"""
import socket
import threading
import urllib.parse

LISTEN = ("127.0.0.1", 3128)


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(c):
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            d = c.recv(4096)
            if not d:
                c.close()
                return
            head += d
            if len(head) > 65536:
                c.close()
                return
        line, rest = head.split(b"\r\n", 1)
        method, target, ver = line.decode("latin-1").split(" ", 2)
        if method.upper() == "CONNECT":
            host, port = target.rsplit(":", 1)
            up = socket.create_connection((host, int(port)), timeout=30)
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            body = head.split(b"\r\n\r\n", 1)[1]
            if body:
                up.sendall(body)
        else:
            u = urllib.parse.urlsplit(target)
            port = u.port or 80
            up = socket.create_connection((u.hostname, port), timeout=30)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            hdrs = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith(b"proxy-")]
            # body may follow the headers
            hdr_part, _, body = rest.partition(b"\r\n\r\n")
            hdrs = [h for h in hdr_part.split(b"\r\n") if h and not h.lower().startswith(b"proxy-")]
            req = f"{method} {path} {ver}\r\n".encode("latin-1") + b"\r\n".join(hdrs) + b"\r\n\r\n" + body
            up.sendall(req)
        up.settimeout(None)
        c.settimeout(None)
        threading.Thread(target=pipe, args=(up, c), daemon=True).start()
        pipe(c, up)
    except Exception:
        try:
            c.close()
        except OSError:
            pass


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(LISTEN)
    s.listen(64)
    print(f"proxy on {LISTEN[0]}:{LISTEN[1]}", flush=True)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
