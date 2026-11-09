"""State model without hardware: fake snapshots, real rule/kernel-log handling."""
from usb_panopticon import daemon, klog


def esp(port="3-1.3.2"):
    return {"vid": "303a", "pid": "1001", "product": "USB JTAG/serial debug unit", "manufacturer": "Espressif",
            "serial": "AA:BB", "speed": "12", "ttys": [{"dev": "/dev/ttyACM0", "by_id": ""}], "msc": [], "ifaces": []}


def make(monkeypatch, snaps):
    monkeypatch.setattr(daemon.usb, "scan", lambda ctx, port: snaps.get(port))
    d = daemon.Daemon()
    d.changed = lambda: None
    d.started = 0
    return d


def test_bootloop_is_one_row_and_flags_looping(monkeypatch):
    d = make(monkeypatch, {"3-1.3.2": esp()})
    p = d.port("3-1.3.2")
    t = 1000.0
    for i in range(4):
        d.connected(p, t + i * 4)
        d.disconnected(p, t + i * 4 + 3)
    d.connected(p, t + 16)
    rows = d.rows(t + 17)
    assert len(rows) == 1
    r = rows[0]
    assert (r["kind"], r["label"], r["reconnects"], r["looping"], r["present"]) == ("mcu", "ESP", 4, True, True)
    assert d.rows(t + 100)[0]["looping"] is False


def test_mode_change_keeps_row(monkeypatch):
    snaps = {"3-2": {**esp(), "vid": "2e8a", "pid": "0009", "ttys": []}}
    d = make(monkeypatch, snaps)
    p = d.port("3-2")
    d.connected(p, 10)
    d.disconnected(p, 20)
    snaps["3-2"] = {**esp(), "vid": "2e8a", "pid": "000f", "ttys": [], "msc": [{"dev": "/dev/sda1", "label": "RP2350"}]}
    d.connected(p, 21)
    r = d.rows(22)[0]
    assert (r["label"], r["mode"], r["prev"]) == ("RP2350", "boot", "RP2 app (2e8a:0009)")


def test_enumeration_errors_without_device(monkeypatch):
    d = make(monkeypatch, {})
    for line in ["usb 3-1-port3: attempt power cycle", "usb 3-1.3: device descriptor read/64, error -71",
                 "usb 3-1-port3: unable to enumerate USB device"]:
        d.on_kernel(line, 500.0)
    rows = d.rows(501)
    assert [(r["port"], r["kind"], r["error_count"]) for r in rows] == [("3-1.3", "error", 2)]
    assert d.alert["text"] == "unable to enumerate USB device"
    assert d.rows(500 + 121) == []      # error_keep_s


def test_gone_and_unmatched(monkeypatch):
    kb = {**esp(), "vid": "04d9", "pid": "a096", "ttys": []}
    d = make(monkeypatch, {"3-1.3.2": esp(), "3-1.3.1": kb})
    d.connected(d.port("3-1.3.2"), 0)
    d.connected(d.port("3-1.3.1"), 0)
    d.disconnected(d.port("3-1.3.2"), 10)
    # gone devices keep their last tty (stable bar label), flagged not present
    assert [(r["port"], r["present"], r["ttys"][0]["dev"]) for r in d.rows(20)] == [("3-1.3.2", False, "/dev/ttyACM0")]
    assert d.rows(71) == []


def test_klog_ports():
    assert klog.parse("usb usb3-port2: over-current condition")[:2] == ("3-2", "error")
    assert klog.parse("usb 3-1.3.2: cannot submit urb 0, error -19: no device")[1] == "info"
    assert klog.parse("spd5118 1-0051: PM: failed to resume async: error -5") is None
    assert klog.port_from_devpath("/devices/pci0000:00/0000:00:14.0/usb3/3-1/3-1.3/3-1.3.2/3-1.3.2:1.0/tty/ttyACM0") == "3-1.3.2"


def test_timeline_carries_state_before_window(monkeypatch):
    d = make(monkeypatch, {"3-2": esp()})
    p = d.port("3-2")
    d.connected(p, 100)            # long before the window
    d.disconnected(p, 1000)
    d.on_kernel("usb 3-2: device descriptor read/64, error -71", 1001)
    d.connected(p, 1002)
    assert p.timeline_since(500) == [[100, "u"], [1000, "d"], [1001, "e"], [1002, "u"]]
    assert p.timeline_since(1000.5) == [[1000, "d"], [1001, "e"], [1002, "u"]]


class Sink:
    def __init__(self):
        self.msgs = []

    def send(self, m):
        self.msgs.append(m)


def test_cycle_writes_disable_and_guards(monkeypatch, tmp_path):
    import asyncio
    kb = {**esp(), "vid": "04d9", "pid": "a096", "ttys": []}
    d = make(monkeypatch, {"3-1.3.2": esp(), "3-1.3.1": kb})
    d.cfg.settings["cycle_off_s"] = 0
    attr = tmp_path / "disable"
    writes = []
    monkeypatch.setattr(daemon.usb, "port_disable_path", lambda port: attr)
    monkeypatch.setattr(daemon.usb, "is_hub", lambda port: False)
    orig = type(attr).write_text
    monkeypatch.setattr(type(attr), "write_text", lambda self, v: (writes.append(v), orig(self, v))[1])
    d.connected(d.port("3-1.3.2"), 0)
    d.connected(d.port("3-1.3.1"), 0)
    import time
    d.on_kernel("usb 3-1.3.1: device descriptor read/64, error -71", time.time())   # keyboard port: error row
    c = Sink()
    asyncio.run(d.cycle(c, "3-1.3.2"))
    assert writes == ["1", "0"] and c.msgs[-1]["ok"]
    assert any(e == "c" for _, e in d.port("3-1.3.2").timeline)
    asyncio.run(d.cycle(c, "3-1.3.1"))
    assert not c.msgs[-1]["ok"] and "unmatched" in c.msgs[-1]["message"]
    asyncio.run(d.cycle(c, "9-9"))
    assert not c.msgs[-1]["ok"] and writes == ["1", "0"]
