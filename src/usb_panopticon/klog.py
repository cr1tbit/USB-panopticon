"""Kernel log lines → (port, severity, text).

Ports are USB topology paths as in /sys/bus/usb/devices: "3-1.3.2" = bus 3, root port 1, hub port 3,
hub port 2. Hub port messages ("usb 3-1-port3: ...", "usb usb3-port2: ...") name the port *below*
the hub, so a board that never enumerates still gets its errors attributed to the right port.
"""
from __future__ import annotations

import re

# "<driver> <device>: <message>"; device = usb port, hub port or interface (port:config.iface).
# Port components are 1-2 digits, which keeps i2c names like "spd5118 1-0051" out.
_PORT = r"\d+-\d{1,2}(?:\.\d{1,2})*"
LINE = re.compile(rf"^(?P<drv>[\w-]+) (?P<dev>usb\d+-port\d+|{_PORT}(?:-port\d+|:\d+\.\d+)?): (?P<msg>.*)$")

ERROR = re.compile("|".join([
    r"error -\d+", r"err = -\d+", r"unable to enumerate", r"cannot enable", r"over-?current",
    r"disabled by hub", r"not accepting address", r"can't set config", r"can't read configurations",
    r"not responding to setup address", r"cannot reset", r"\bfail(ed|ure)\b", r"device not responding",
]), re.I)
WARN = re.compile(r"reset \S+-speed USB device|not running at top speed|attempt power cycle", re.I)
# expected while a device is being unplugged: ESHUTDOWN / ENODEV / ENOENT
NOISE = re.compile(r"error -(108|19|2)\b|no device\b|-ENODEV", re.I)


def port_of(dev: str) -> str:
    if m := re.fullmatch(r"usb(\d+)-port(\d+)", dev):
        return f"{m[1]}-{m[2]}"
    if m := re.fullmatch(rf"({_PORT})-port(\d+)", dev):
        return f"{m[1]}.{m[2]}"
    return dev.split(":", 1)[0]


def severity(msg: str) -> str:
    if NOISE.search(msg):
        return "info"
    if ERROR.search(msg):
        return "error"
    if WARN.search(msg):
        return "warn"
    return "info"


def parse(line: str) -> tuple[str, str, str] | None:
    """Returns (port, severity, text) for USB-related lines, else None."""
    m = LINE.match(line.strip())
    if not m:
        return None
    return port_of(m["dev"]), severity(m["msg"]), f"{m['drv']}: {m['msg']}" if m["drv"] != "usb" else m["msg"]


def port_from_devpath(devpath: str) -> str | None:
    """Innermost USB port in a sysfs DEVPATH (works for removed devices too)."""
    for part in reversed(devpath.split("/")):
        if re.fullmatch(_PORT, part):
            return part
    return None
