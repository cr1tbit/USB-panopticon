# usb-panopticon

Bench USB watcher for board bring-up: replaces keeping `dmesg -wH` open. A DMS (niri bar) widget lists
MCU / USB-UART / serial devices with how long they've been up, flags re-enumeration loops (bootlooping
native-USB ESPs), groups debug probes separately, and flashes USB errors (`-71`, `unable to enumerate`,
over-current, …) on the port where they happened, even if no device ever enumerated.

## How it works

```
udev netlink (usb/tty/block uevents) ─┐
                                      ├─► daemon ── $XDG_RUNTIME_DIR/usb-panopticon.sock (JSON lines) ──► DMS widget / CLI
journalctl -k -f -o json (kernel log)─┘    per-port state
```

- **Rows are keyed by physical USB port** (`3-1.3.2` as in `/sys/bus/usb/devices`). A board that
  re-enumerates (bootloop, app ↔ BOOTSEL) stays one row with a reconnect counter, and a mode change
  shows what it was before ("was RP2 app"). ≥ `loop_count` connects within `loop_window_s` = LOOPING.
- **Kernel log**: lines are attributed to ports, including hub-port messages (`usb 3-1-port3: …` →
  `3-1.3`) so enumeration failures of a dead board land on its port. Clicking a card shows that port's
  kernel log, effectively a per-port `dmesg`. The last 120 s are replayed at daemon start.
  `/dev/kmsg` is root-only here (`dmesg_restrict=1`); the journal is readable via the `wheel` group.
- Both sources are event-driven: idle cost is zero CPU, ~30 MB RSS (Python). Updates are pushed to the
  widget within ~30 ms of the event.
- **Limitation:** an ESP behind a USB-UART bridge (CP210x/CH340) that bootloops doesn't re-enumerate,
  so it can't be seen here (opening the port to watch would reset it via DTR/RTS).

## Rules

`~/.config/usb-panopticon/config.toml`, created from `src/usb_panopticon/default_config.toml` on first
run and reloaded on change. First-match rules on `vid:pid` glob (+ optional product regex,
`has = "tty"|"msc"`) give a label, a kind (`mcu`, `bridge`, `probe`, `serial`, `ignore`) and a mode
(`app`/`boot`). Unmatched devices are hidden; USB errors are shown for any port (`ignore_ports` to
silence one). A broken config falls back to the default and the error shows in the popout.

## Bar entry mouse actions

- left click: popout (opens after the double-click interval)
- double left click: run `terminal_cmd` from the config, `{port}` = the device's first tty (shell-quoted),
  default `alacritty -e picocom {port} --baud 115200`
- hold right button 1 s: port cycle (below)

## Port cycle

Expand a card in the popout → "cycle port", or hold the right mouse button on a bar entry for 1 s.
The daemon writes `1`, waits `cycle_off_s`, then `0` to the hub port's sysfs `disable` attribute
(Linux ≥ 6.0). On hubs with per-port power switching that cuts VBUS (a real power cycle); on others it is
a logical disconnect + fresh enumeration (the board stays powered). Deliberately USB-layer only: no
DTR/RTS or picotool resets, which belong to serial monitors / debug tools.
Refused for ports not listed in the widget, hubs, and ports carrying an unmatched device (keyboard etc.).
Cycles show as secondary-coloured ticks on the strips. Needs the udev rule below (group `plugdev`).

## Install

```sh
uv sync
install -Dm644 systemd/usb-panopticon.service ~/.config/systemd/user/usb-panopticon.service
systemctl --user daemon-reload && systemctl --user enable --now usb-panopticon
ln -s "$PWD/dms-plugin" ~/.config/DankMaterialShell/plugins/usb-panopticon
# port cycle permission (hub port 'disable' attributes writable by plugdev):
sudo install -m644 udev/71-usb-panopticon.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --action=change
```

Then enable "USB Panopticon" in DMS settings ▸ Plugins and add it to the bar.

## CLI

```sh
usb-panopticon ls                    # what the widget shows
usb-panopticon watch                 # connects / disconnects / errors as they happen
usb-panopticon wait esp --new        # print the tty of the next ESP that connects
espflash flash --port "$(usb-panopticon wait esp --new --timeout 30)" app.bin
usb-panopticon clear                 # forget gone devices, errors, counters
usb-panopticon cycle 3-1.3.2         # port cycle from the terminal
```

Tests (no hardware needed): `uv run pytest`.
