"""usb-panopticon daemon: udev netlink + kernel log → per-port state, JSON lines on a Unix socket.

Rows are keyed by physical USB port, so a bootlooping board or a BOOTSEL/app switch stays one row
(with a connect counter), and enumeration errors land on the port even when no device ever appears.

client -> daemon   {"op": "hello", "role": "widget"|"cli"} (answered with a state)   {"op": "clear"}
                   {"op": "cycle", "port": "3-1.3.2"}  disable + re-enable the hub port (answered with cycle_result)
daemon -> client   {"type": "state", "now": t, "rows": [...], "alert": {...}|null, "bus_timeline": [[t, "e"], ...], ...}
                   (sent after hello and after every change, coalesced to one message per 30 ms)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque

import pyudev

from . import config, klog, usb
from .paths import SOCKET

PUSH_DELAY = 0.03
HISTORY = 60
TIMELINE_S = 600          # how much connect/disconnect/error history rows carry (popout strip)


class Port:
    def __init__(self, name: str):
        self.name = name
        self.dev: dict | None = None      # last snapshot (kept after removal)
        self.present = False
        self.since = 0.0                  # last connect
        self.gone_at = 0.0
        self.connects: deque[float] = deque(maxlen=50)
        self.reconnects = 0               # connects seen while the daemon runs, after the first
        self.prev_ident = ""              # identity before a vid:pid change (e.g. app → BOOTSEL)
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.errors: deque[dict] = deque(maxlen=HISTORY)
        self.timeline: list[tuple[float, str]] = []   # (t, 'u'|'d'|'e'|'c'): connected/disconnected/error/port cycled
        self.cycling = False

    def log(self, t: float, sev: str, text: str, src: str):
        e = {"t": t, "sev": sev, "text": text, "src": src}
        self.history.append(e)
        if sev == "error":
            self.errors.append(e)
            self.mark(t, "e")

    def mark(self, t: float, ev: str):
        self.timeline.append((t, ev))
        if len(self.timeline) > 1000:
            del self.timeline[:500]

    def timeline_since(self, cutoff: float) -> list:
        """Events after cutoff, plus the last connect/disconnect before it (the state the strip starts in)."""
        tl = sorted(self.timeline)
        recent = [[t, e] for t, e in tl if t >= cutoff]
        before = [[t, e] for t, e in tl if t < cutoff and e in "ud"]
        return before[-1:] + recent


class Client:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.role = "cli"

    def send(self, msg: dict):
        try:
            self.writer.write((json.dumps(msg) + "\n").encode())
        except Exception:
            pass


class Daemon:
    def __init__(self):
        self.cfg, self.cfg_error = config.load()
        self.cfg_mtime = config.mtime()
        self.ctx = pyudev.Context()
        self.ports: dict[str, Port] = {}
        self.clients: set[Client] = set()
        self.alert: dict | None = None
        self.push_handle: asyncio.TimerHandle | None = None
        self.started = time.time()
        self.last_sig = None

    # ------------------------------------------------------------ state

    def port(self, name: str) -> Port:
        if name not in self.ports:
            self.ports[name] = Port(name)
        return self.ports[name]

    def rescan(self, p: Port):
        snap = usb.scan(self.ctx, p.name)
        if snap:
            p.dev = snap

    def connected(self, p: Port, t: float, initial=False):
        old = p.dev
        p.present, p.since, p.gone_at = True, t, 0.0
        self.rescan(p)
        if p.connects and not initial:
            p.reconnects += 1
        p.connects.append(t)
        p.mark(t, "u")
        if initial:
            return
        if old and p.dev and (old["vid"], old["pid"]) != (p.dev["vid"], p.dev["pid"]):
            p.prev_ident = self.ident(old)
        ident = self.ident(p.dev) if p.dev else "?"
        p.log(t, "info", f"▲ connected {ident}", "udev")

    def disconnected(self, p: Port, t: float):
        if p.present:
            p.present, p.gone_at = False, t
            p.mark(t, "d")
            p.log(t, "info", "▼ disconnected", "udev")

    def ident(self, dev: dict) -> str:
        r = self.cfg.classify(dev)
        name = (r.label if r and r.label else "") or dev.get("product") or f"{dev['vid']}:{dev['pid']}"
        return f"{name}{' ' + r.mode if r and r.mode else ''} ({dev['vid']}:{dev['pid']})"

    def row(self, p: Port, now: float) -> dict | None:
        s = self.cfg.settings
        if p.name in s["ignore_ports"]:
            return None
        rule = self.cfg.classify(p.dev) if p.dev else None
        recent_err = [e for e in p.errors if now - e["t"] < s["error_keep_s"]]
        shown_dev = rule and rule.kind != "ignore" and (p.present or now - p.gone_at < s["gone_keep_s"])
        if not shown_dev and not recent_err:
            return None
        d = p.dev or {}
        looping = sum(1 for c in p.connects if now - c < s["loop_window_s"]) >= s["loop_count"]
        return {
            "port": p.name,
            "kind": rule.kind if shown_dev else "error",
            "label": (rule.label if rule else "") or d.get("product") or (f"{d['vid']}:{d['pid']}" if d else p.name),
            "mode": rule.mode if rule else "",
            "vid": d.get("vid", ""), "pid": d.get("pid", ""), "product": d.get("product", ""),
            "manufacturer": d.get("manufacturer", ""), "serial": d.get("serial", ""), "speed": d.get("speed", ""),
            # last known nodes are kept for gone devices (stable bar label); check "present" before using them
            "ttys": d.get("ttys", []), "msc": d.get("msc", []),
            "drivers": sorted({i["driver"] or "(none)" for i in d.get("ifaces", [])}) if p.present else [],
            "present": p.present, "since": p.since, "gone_at": p.gone_at,
            "reconnects": p.reconnects, "looping": looping, "prev": p.prev_ident, "cycling": p.cycling,
            "last_error": recent_err[-1] if recent_err else None, "error_count": len(recent_err),
            "history": list(p.history)[-40:],
            "timeline": p.timeline_since(now - TIMELINE_S),
        }

    def rows(self, now: float) -> list[dict]:
        order = {"mcu": 0, "bridge": 1, "serial": 2, "error": 3, "probe": 4}
        rows = [r for p in self.ports.values() if (r := self.row(p, now))]
        return sorted(rows, key=lambda r: (order.get(r["kind"], 9), not r["present"], -r["since"]))

    def state(self) -> dict:
        now = time.time()
        ignored = self.cfg.settings["ignore_ports"]
        bus = sorted([e["t"], "e"] for p in self.ports.values() if p.name not in ignored
                     for e in p.errors if now - e["t"] < TIMELINE_S)
        return {"type": "state", "now": now, "rows": self.rows(now), "alert": self.alert, "bus_timeline": bus,
                "alert_s": self.cfg.settings["alert_s"], "bar_max_chips": self.cfg.settings["bar_max_chips"],
                "config_error": self.cfg_error, "config_path": str(config.PATH)}

    def changed(self):
        if self.push_handle is None:
            self.push_handle = asyncio.get_running_loop().call_later(PUSH_DELAY, self.push)

    def push(self):
        self.push_handle = None
        msg = self.state()
        self.last_sig = self.signature(msg)
        for c in list(self.clients):
            c.send(msg)

    @staticmethod
    def signature(msg: dict):
        return tuple((r["port"], r["kind"], r["present"], r["looping"], r["error_count"]) for r in msg["rows"])

    # ------------------------------------------------------------ sources

    def snapshot(self):
        for name in usb.present_ports():
            self.connected(self.port(name), usb.initialized_at(self.ctx, name), initial=True)

    def on_uevent(self, dev: pyudev.Device):
        name = klog.port_from_devpath(dev.device_path)
        if not name:
            return
        p, t = self.port(name), time.time()
        if dev.subsystem == "usb" and dev.device_type == "usb_device":
            if dev.action == "add":
                self.connected(p, t)
            elif dev.action == "remove":
                self.disconnected(p, t)
            else:
                self.rescan(p)
        elif p.present:
            self.rescan(p)   # interface bind, tty / block node appeared or vanished
        self.changed()

    def on_kernel(self, line: str, t: float):
        parsed = klog.parse(line)
        if not parsed:
            return
        name, sev, text = parsed
        p = self.port(name)
        p.log(t, sev, text, "kernel")
        if sev == "error" and t >= self.started - 1 and name not in self.cfg.settings["ignore_ports"]:
            self.alert = {"t": t, "port": name, "text": text}
        self.changed()

    async def watch_udev(self):
        mon = pyudev.Monitor.from_netlink(self.ctx)
        for sub in ("usb", "tty", "block"):
            mon.filter_by(sub)
        mon.set_receive_buffer_size(4 * 1024 * 1024)
        mon.start()

        def readable():
            while (dev := mon.poll(timeout=0)) is not None:
                try:
                    self.on_uevent(dev)
                except Exception as e:
                    print(f"uevent {dev}: {e}", flush=True)

        asyncio.get_running_loop().add_reader(mon.fileno(), readable)
        await asyncio.Event().wait()

    async def watch_kernel(self):
        since = "-120s"
        while True:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-k", "-f", "-q", "-o", "json", "--output-fields=MESSAGE", f"--since={since}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)
            assert proc.stdout
            while line := await proc.stdout.readline():
                try:
                    e = json.loads(line)
                    msg = e["MESSAGE"]
                    if isinstance(msg, list):     # non-UTF-8 messages come as byte arrays
                        msg = bytes(msg).decode(errors="replace")
                    self.on_kernel(msg, int(e["__REALTIME_TIMESTAMP"]) / 1e6)
                except Exception:
                    continue
            await proc.wait()
            print("journalctl exited, restarting", flush=True)
            since = "-5s"
            await asyncio.sleep(1)

    async def housekeeping(self):
        """Expire gone devices / old errors / loop flags, and reload the config when it changes."""
        while True:
            await asyncio.sleep(1)
            if (m := config.mtime()) != self.cfg_mtime:
                self.cfg_mtime = m
                self.cfg, self.cfg_error = config.load()
                print(f"config reloaded{': ' + self.cfg_error if self.cfg_error else ''}", flush=True)
                self.changed()
            elif self.clients and self.signature(self.state()) != self.last_sig:
                self.changed()

    # ------------------------------------------------------------ clients

    async def serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client = Client(writer)
        self.clients.add(client)
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("op") == "hello":
                    client.role = msg.get("role", "cli")
                    client.send(self.state())     # after hello: the widget attaches its parser on link-up
                elif msg.get("op") == "clear":
                    self.clear()
                elif msg.get("op") == "cycle":
                    asyncio.create_task(self.cycle(client, str(msg.get("port", ""))))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients.discard(client)
            writer.close()

    async def cycle(self, client: Client, name: str):
        """Disable + re-enable the hub port: VBUS is cut only on hubs with per-port power switching,
        otherwise it is a logical disconnect and fresh enumeration. Only for ports the widget shows."""
        def reply(ok: bool, msg: str):
            client.send({"type": "cycle_result", "port": name, "ok": ok, "message": msg})

        p = self.ports.get(name)
        if not p or not self.row(p, time.time()):
            return reply(False, f"{name} is not a listed port")
        if p.cycling:
            return reply(False, f"{name} is already being cycled")
        if p.present and p.dev is None:
            return reply(False, f"{name}: unknown device")
        if p.present and usb.is_hub(name):
            return reply(False, f"{name} is a hub, refusing to cut everything behind it")
        if p.present and not ((r := self.cfg.classify(p.dev)) and r.kind != "ignore"):
            return reply(False, f"{name} carries an unmatched device ({p.dev['vid']}:{p.dev['pid']} {p.dev.get('product', '')}), refusing")
        path = usb.port_disable_path(name)
        if not path:
            return reply(False, f"no hub port control for {name} (kernel < 6.0?)")
        off = float(self.cfg.settings["cycle_off_s"])
        p.cycling = True
        t = time.time()
        p.log(t, "info", f"⏻ port cycle: off {off:g} s", "user")
        p.mark(t, "c")
        self.changed()
        try:
            await asyncio.to_thread(path.write_text, "1")
            await asyncio.sleep(off)
            await asyncio.to_thread(path.write_text, "0")
            reply(True, f"{name} cycled")
        except PermissionError:
            p.log(time.time(), "warn", "port cycle failed: permission denied (install udev/71-usb-panopticon.rules)", "user")
            reply(False, f"permission denied on {path} — install udev/71-usb-panopticon.rules")
        except OSError as e:
            try:
                await asyncio.to_thread(path.write_text, "0")    # never leave a port disabled
            except OSError:
                pass
            p.log(time.time(), "warn", f"port cycle failed: {e}", "user")
            reply(False, f"{name}: {e}")
        finally:
            p.cycling = False
            self.changed()

    def clear(self):
        """Forget disconnected devices, errors and counters."""
        for name, p in list(self.ports.items()):
            if not p.present:
                del self.ports[name]
                continue
            p.errors.clear()
            p.connects.clear()
            p.connects.append(p.since)
            p.timeline = [(p.since, "u")]
            p.reconnects, p.prev_ident = 0, ""
        self.alert = None
        self.changed()

    async def run(self):
        SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.serve_client, path=str(SOCKET), limit=1024 * 1024)
        os.chmod(SOCKET, 0o600)
        # start listening before the snapshot so nothing falls between the two
        tasks = [asyncio.create_task(self.watch_udev())]
        await asyncio.sleep(0)
        self.snapshot()
        tasks += [asyncio.create_task(self.watch_kernel()), asyncio.create_task(self.housekeeping())]
        if self.cfg_error:
            print(f"config error: {self.cfg_error}", flush=True)
        print(f"usb-panopticon listening on {SOCKET}", flush=True)
        async with server:
            await server.serve_forever()


def main():
    try:
        asyncio.run(Daemon().run())
    except KeyboardInterrupt:
        pass
