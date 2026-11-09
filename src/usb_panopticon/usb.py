"""Snapshot of one USB device (by port) from sysfs + udev properties."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pyudev

SYS = Path("/sys/bus/usb/devices")


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _props(ctx: pyudev.Context, subsystem: str, name: str) -> dict:
    try:
        return dict(pyudev.Devices.from_name(ctx, subsystem, name).properties)
    except Exception:
        return {}


def scan(ctx: pyudev.Context, port: str) -> dict | None:
    base = SYS / port
    vid = _read(base / "idVendor")
    if not vid:
        return None
    dev = {"vid": vid, "pid": _read(base / "idProduct"), "product": _read(base / "product"),
           "manufacturer": _read(base / "manufacturer"), "serial": _read(base / "serial"),
           "speed": _read(base / "speed"), "ttys": [], "msc": [], "ifaces": []}
    real = base.resolve()
    for iface in sorted(real.glob(f"{port}:*")):
        drv = iface / "driver"
        dev["ifaces"].append({"cls": _read(iface / "bInterfaceClass"),
                              "driver": os.path.basename(os.readlink(drv)) if drv.is_symlink() else ""})
        for t in [*iface.glob("tty/tty*"), *iface.glob("tty[A-Z]*")]:
            links = _props(ctx, "tty", t.name).get("DEVLINKS", "").split()
            by_id = next((l for l in links if "/by-id/" in l), "")
            dev["ttys"].append({"dev": f"/dev/{t.name}", "by_id": by_id})
        for disk in iface.glob("host*/target*/*/block/*"):
            for blk in [disk, *disk.glob(f"{disk.name}*")]:
                label = _props(ctx, "block", blk.name).get("ID_FS_LABEL", "")
                if label or blk == disk:
                    dev["msc"].append({"dev": f"/dev/{blk.name}", "label": label})
    # drop the bare disk when a labelled partition exists
    if any(m["label"] for m in dev["msc"]):
        dev["msc"] = [m for m in dev["msc"] if m["label"]]
    return dev


def present_ports() -> list[str]:
    return sorted(p.name for p in SYS.iterdir() if ":" not in p.name and not p.name.startswith("usb"))


def initialized_at(ctx: pyudev.Context, port: str) -> float:
    """Wall-clock time udev first saw the device (USEC_INITIALIZED is CLOCK_MONOTONIC µs)."""
    try:
        usec = int(pyudev.Devices.from_name(ctx, "usb", port).properties.get("USEC_INITIALIZED", "0"))
    except Exception:
        usec = 0
    if not usec:
        return time.time()
    return time.time() - (time.monotonic() - usec / 1e6)


def is_hub(port: str) -> bool:
    return _read(SYS / port / "bDeviceClass") == "09"


def port_disable_path(port: str) -> Path | None:
    """sysfs 'disable' attribute of the hub port a device hangs off (3-1.3.2 → hub 3-1.3, port 2;
    3-2 → root hub usb3, port 2)."""
    bus, chain = port.split("-", 1)
    if "." in chain:
        hub, num = port.rsplit(".", 1)
        name = f"{hub}-port{num}"
    else:
        hub, name = f"usb{bus}", f"usb{bus}-port{chain}"
    return next(iter((SYS / hub).glob(f"*:1.0/{name}/disable")), None)
