"""usb-panopticon CLI.

  usb-panopticon daemon                         run the daemon
  usb-panopticon ls                             list shown devices, probes and ports with recent errors
  usb-panopticon watch                          print connects, disconnects and USB errors as they happen
  usb-panopticon wait [PATTERN] [--new] [--timeout S]
                                                print the serial port (or block device / USB port) of the
                                                first present device matching PATTERN, waiting for it if
                                                needed. PATTERN is a case-insensitive substring of label,
                                                product, kind, mode, vid:pid, port or tty. --new ignores
                                                devices connected before the command started.
                                                e.g. espflash flash --port "$(usb-panopticon wait esp --new)" app.bin
  usb-panopticon clear                          forget disconnected devices, errors and counters
  usb-panopticon cycle PORT                     disable + re-enable the hub port of a listed device / error port
                                                (VBUS is cut only on hubs with per-port power switching)
"""
from __future__ import annotations

import json
import socket
import sys
import time

from .paths import SOCKET


def connect() -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCKET))
    except OSError:
        sys.exit(f"daemon not running ({SOCKET})")
    s.sendall(b'{"op": "hello", "role": "cli"}\n')
    return s


def states(s: socket.socket):
    buf = b""
    while chunk := s.recv(1 << 20):
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            m = json.loads(line)
            if m.get("type") == "state":
                yield m


def age(s: float) -> str:
    s = max(0, int(s))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def endpoint(r: dict) -> str:
    if r["ttys"]:
        return r["ttys"][0]["dev"]
    if r["msc"]:
        return r["msc"][0]["dev"]
    return r["port"]


def describe(r: dict, now: float) -> str:
    status = (f"up {age(now - r['since'])}" if r["present"] else f"gone {age(now - r['gone_at'])}") if r["kind"] != "error" else "no device"
    bits = [f"{r['kind']:<6}", f"{r['port']:<10}", f"{r['label']}{' ' + r['mode'] if r['mode'] else ''}"]
    if r["vid"]:
        bits.append(f"{r['vid']}:{r['pid']}")
    bits.append(status)
    if r["present"] and (r["ttys"] or r["msc"]):
        bits.append(" ".join([t["dev"] for t in r["ttys"]] + [f"{m['dev']}[{m['label']}]" if m["label"] else m["dev"] for m in r["msc"]]))
    if r["reconnects"]:
        bits.append(f"{r['reconnects']}× reconnected" + (" LOOPING" if r["looping"] else ""))
    if r["last_error"]:
        bits.append(f"{r['error_count']} err, last: {r['last_error']['text']}")
    return "  ".join(bits)


def matches(r: dict, pat: str) -> bool:
    hay = " ".join([r["label"], r["product"], r["kind"], r["mode"], f"{r['vid']}:{r['pid']}", r["port"],
                    *[t["dev"] for t in r["ttys"]]]).lower()
    return pat.lower() in hay


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
    elif args[0] == "daemon":
        from .daemon import main as daemon_main
        daemon_main()
    elif args[0] == "ls":
        st = next(states(connect()))
        for r in st["rows"]:
            print(describe(r, st["now"]))
        if st["config_error"]:
            print(f"config error: {st['config_error']}", file=sys.stderr)
    elif args[0] == "clear":
        s = connect()
        s.sendall(b'{"op": "clear"}\n')
        next(states(s))
    elif args[0] == "cycle" and len(args) == 2:
        s = connect()
        s.sendall((json.dumps({"op": "cycle", "port": args[1]}) + "\n").encode())
        buf = b""
        while chunk := s.recv(1 << 20):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                m = json.loads(line)
                if m.get("type") == "cycle_result":
                    print(m["message"])
                    sys.exit(0 if m["ok"] else 1)
    elif args[0] == "watch":
        seen: dict[str, tuple] = {}
        last_alert = None
        for st in states(connect()):
            now = st["now"]
            for r in st["rows"]:
                key = (r["present"], r["since"], tuple(t["dev"] for t in r["ttys"]), r["looping"])
                if seen.get(r["port"]) != key:
                    print(time.strftime("%H:%M:%S"), describe(r, now), flush=True)
                    seen[r["port"]] = key
            if st["alert"] and st["alert"] != last_alert:
                last_alert = st["alert"]
                print(time.strftime("%H:%M:%S"), f"ERROR {last_alert['port']}: {last_alert['text']}", flush=True)
    elif args[0] == "wait":
        rest, new, timeout, pat = args[1:], "--new" in args, None, ""
        if "--timeout" in rest:
            i = rest.index("--timeout")
            timeout = float(rest[i + 1])
            del rest[i:i + 2]
        pat = " ".join(a for a in rest if a != "--new")
        start = time.time()
        s = connect()
        if timeout:
            s.settimeout(timeout)
        try:
            for st in states(s):
                for r in st["rows"]:
                    # a device is usable once its tty / block node exists (or it has none to wait for)
                    ready = (r["present"] and (r["ttys"] or r["msc"])) or (st["now"] - r["since"] > 1.5)
                    if r["present"] and r["kind"] != "error" and (not new or r["since"] >= start) and matches(r, pat) and ready:
                        print(endpoint(r))
                        return
                if timeout and time.time() - start > timeout:
                    break
        except TimeoutError:
            pass
        sys.exit(f"no matching device within {timeout:g}s" if timeout else "daemon went away")
    else:
        sys.exit(__doc__)
