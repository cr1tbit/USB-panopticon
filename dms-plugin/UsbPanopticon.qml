import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.Widgets
import qs.DCommon.Common as DCC
import qs.DCommon.Widgets as DW
import qs.Modules.Plugins

// Bench USB watcher. All logic lives in the daemon (usb-panopticon daemon); this only renders its state.
PluginComponent {
    id: root

    property var st: ({})
    readonly property var rows: st.rows || []
    readonly property bool linked: sock.linkUp
    property real now: Date.now() / 1000
    property var expanded: ({})

    readonly property var devRows: rows.filter(r => r.kind !== "probe" && r.kind !== "error")
    readonly property var probeRows: rows.filter(r => r.kind === "probe")
    readonly property var errRows: rows.filter(r => r.kind === "error")
    readonly property var barRows: devRows.slice(0, st.bar_max_chips || 4)
    readonly property int hiddenOnBar: devRows.length - barRows.length
    readonly property var busRow: ({ "timeline": st.bus_timeline || [], "present": false })
    readonly property bool busRecent: linked && (st.bus_timeline || []).some(e => now - e[0] < (st.alert_s || 5))
    // bar labels only ever grow per port, so a tty / mode change never shifts the bar
    property var labelW: ({})
    property var barPorts: []
    readonly property var rowMap: {
        const m = {};
        for (const r of rows)
            m[r.port] = r;
        return m;
    }

    function cycle(port) {
        sock.send({ "op": "cycle", "port": port });
    }
    readonly property int probesUp: probeRows.filter(r => r.present).length
    readonly property bool alerting: linked && !!st.alert && now - st.alert.t < (st.alert_s || 5)

    function receive(line) {
        try {
            const msg = JSON.parse(line);
            if (msg.type === "state") {
                st = msg;
                now = Date.now() / 1000;
                // bar delegates are keyed by port and only rebuilt when the set of ports changes,
                // so an entry held with the right button survives the frequent state pushes of a looping board
                const ports = barRows.map(r => r.port);
                if (ports.join(" ") !== barPorts.join(" "))
                    barPorts = ports;
            } else if (msg.type === "cycle_result") {
                if (msg.ok)
                    ToastService.showInfo("USB port " + msg.message);
                else
                    ToastService.showError("Port cycle refused", msg.message);
            }
        } catch (e) {}
    }

    function age(s) {
        s = Math.max(0, Math.floor(s));
        return s < 60 ? s + "s" : s < 3600 ? Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") : Math.floor(s / 3600) + "h" + String(Math.floor(s % 3600 / 60)).padStart(2, "0");
    }

    function shortErr(text) {
        const m = /(error|err =) (-\d+)/.exec(text || "");
        if (m)
            return m[2];
        return (text || "").length > 22 ? text.slice(0, 21) + "…" : text;
    }

    function recentError(r) {
        return !!r.last_error && now - r.last_error.t < (st.alert_s || 5);
    }

    function kindIcon(r) {
        return r.kind === "mcu" ? (r.mode === "boot" ? "system_update_alt" : "memory") : r.kind === "bridge" ? "cable" : r.kind === "probe" ? "bug_report" : r.kind === "error" ? "usb_off" : "terminal";
    }

    function rowColor(r) {
        if (r.kind === "error" || recentError(r))
            return Theme.error;
        if (!r.present)
            return Theme.surfaceVariantText;
        if (r.looping)
            return Theme.warning;
        if (now - r.since < 60)
            return Theme.primary;
        return Theme.surfaceText;
    }

    function growLabel(port, w) {
        if ((labelW[port] || 0) >= w)
            return;
        const m = Object.assign({}, labelW);
        m[port] = w;
        labelW = m;
    }

    function ttyShort(r) {
        return r.ttys.length ? r.ttys[0].dev.replace("/dev/tty", "") : r.msc.length ? (r.msc[0].label || r.msc[0].dev.replace("/dev/", "")) : "";
    }

    function chipText(r) {
        if (r.kind === "error")
            return r.port + " " + shortErr(r.last_error ? r.last_error.text : "");
        let t = r.label + (r.mode === "boot" ? "↓" : "");
        const tty = ttyShort(r);
        if (tty)
            t += " " + tty;
        return t;
    }

    function status(r) {
        if (r.kind === "error")
            return "no device";
        return r.present ? "up " + age(now - r.since) : "gone " + age(now - r.gone_at);
    }

    // segments of connected time inside [now - windowS, now], from the row's timeline
    function segments(r, windowS) {
        const n = now, from = n - windowS;
        const ud = (r.timeline || []).filter(e => e[1] === "u" || e[1] === "d");
        // state at the left edge: the event carried from before the window, else the opposite of the first one
        let up = ud.length ? (ud[0][0] < from ? ud[0][1] === "u" : ud[0][1] === "d") : r.present;
        let cur = up ? from : null;
        const segs = [];
        for (const [t, e] of ud) {
            if (t < from)
                continue;
            if (e === "u" && cur === null)
                cur = t;
            else if (e === "d" && cur !== null) {
                segs.push([cur, t]);
                cur = null;
            }
        }
        if (cur !== null)
            segs.push([cur, r.present ? n : Math.max(cur, r.gone_at || n)]);
        return segs;
    }

    function paintStrip(ctx, W, H, r, windowS, barH) {
        ctx.reset();
        if (!r || W <= 0)
            return;
        const n = now;
        const x = t => Math.max(0, Math.min(W, W - (n - t) / windowS * W));
        ctx.fillStyle = Theme.outlineVariant;
        ctx.fillRect(0, H / 2 - 0.5, W, 1);
        const y = (H - barH) / 2;
        for (const [a, b] of segments(r, windowS)) {
            const ongoing = r.present && b >= n;
            // fresh = bright primary, stable = calm, past spans / gone = faint, looping = warning
            ctx.fillStyle = r.looping ? Theme.warning : !r.present || !ongoing ? Theme.withAlpha(Theme.surfaceVariantText, 0.5) : n - a < 60 ? Theme.primary : Theme.withAlpha(Theme.surfaceText, 0.45);
            const xa = x(a), xb = x(b);
            ctx.fillRect(xa, y, Math.max(1.5, xb - xa), barH);
        }
        // ticks: red = USB error, secondary = port cycled by you
        for (const [t, e] of (r.timeline || [])) {
            if ((e === "e" || e === "c") && t >= n - windowS) {
                ctx.fillStyle = e === "e" ? Theme.error : Theme.secondary;
                ctx.fillRect(Math.min(W - 1.5, x(t) - 0.75), 0, 1.5, H);
            }
        }
    }

    function copy(text) {
        Quickshell.execDetached(["wl-copy", text]);
        ToastService.showInfo("Copied " + text);
    }

    function toggle(port) {
        const e = Object.assign({}, expanded);
        e[port] = !e[port];
        expanded = e;
    }

    function clock(t) {
        return new Date(t * 1000).toTimeString().slice(0, 8);
    }

    DCC.DSocket {
        id: sock
        path: Quickshell.env("XDG_RUNTIME_DIR") + "/usb-panopticon.sock"
        connected: true
        reconnectMaxMs: 3000
        parser: null
        onLinkUpChanged: {
            if (linkUp) {
                parser = parserComponent.createObject(sock);
                send({ "op": "hello", "role": "widget" });
            } else {
                root.st = {};
            }
        }

        Component {
            id: parserComponent
            SplitParser {
                onRead: line => root.receive(line)
            }
        }
    }

    // ages / alert expiry
    Timer {
        interval: 1000
        repeat: true
        running: root.linked && root.rows.length > 0
        onTriggered: root.now = Date.now() / 1000
    }

    // Constant width: one entry per detected device + one bus-error entry; events only change colours.
    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingS

            Repeater {
                model: root.barPorts
                delegate: Item {
                    id: entry
                    required property string modelData
                    readonly property var r: root.rowMap[modelData] || null
                    property real hold: 0          // right-button hold progress, 1 = cycle the port
                    visible: r !== null
                    width: inner.implicitWidth
                    height: inner.implicitHeight
                    anchors.verticalCenter: parent.verticalCenter

                    Row {
                        id: inner
                        spacing: 2
                        DW.DIcon {
                            name: entry.r ? root.kindIcon(entry.r) : ""
                            size: Theme.iconSize - 8
                            color: entry.hold > 0 ? Theme.warning : entry.r ? root.rowColor(entry.r) : "transparent"
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        StyledText {
                            text: entry.r ? root.chipText(entry.r) : ""
                            width: Math.max(implicitWidth, root.labelW[entry.modelData] || 0)
                            onImplicitWidthChanged: root.growLabel(entry.modelData, implicitWidth)
                            color: entry.hold > 0 ? Theme.warning : entry.r ? root.rowColor(entry.r) : "transparent"
                            font.pixelSize: Theme.fontSizeSmall
                            font.strikeout: !!entry.r && !entry.r.present
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Item {
                            width: 2
                            height: 1
                        }
                        Strip {
                            pc: root
                            r: entry.r
                            width: 30
                            height: Theme.iconSize - 6
                            windowS: 60
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    // hold progress, drawn under the entry (no layout change)
                    Rectangle {
                        visible: entry.hold > 0
                        anchors.left: parent.left
                        anchors.top: parent.bottom
                        anchors.topMargin: 1
                        width: parent.width * entry.hold
                        height: 2
                        radius: 1
                        color: Theme.warning
                    }

                    MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onPressed: holdAnim.restart()
                        onReleased: {
                            holdAnim.stop();
                            entry.hold = 0;
                        }
                        onCanceled: {
                            holdAnim.stop();
                            entry.hold = 0;
                        }
                    }

                    NumberAnimation {
                        id: holdAnim
                        target: entry
                        property: "hold"
                        from: 0
                        to: 1
                        duration: 1000
                        onFinished: {
                            if (entry.hold >= 1 && entry.r)
                                root.cycle(entry.modelData);
                            entry.hold = 0;
                        }
                    }
                }
            }

            StyledText {
                visible: root.hiddenOnBar > 0
                text: "+" + root.hiddenOnBar
                color: Theme.surfaceVariantText
                font.pixelSize: Theme.fontSizeSmall
                anchors.verticalCenter: parent.verticalCenter
            }

            DW.DIcon {
                visible: root.probesUp > 0
                name: "bug_report"
                size: Theme.iconSize - 8
                color: Theme.surfaceVariantText
                anchors.verticalCenter: parent.verticalCenter
            }

            // bus errors (any port): always present, red icon right after an error, ticks on its strip
            Row {
                spacing: 2
                anchors.verticalCenter: parent.verticalCenter
                DW.DIcon {
                    name: root.linked ? "usb" : "usb_off"
                    size: Theme.iconSize - 8
                    color: root.busRecent ? Theme.error : Theme.surfaceVariantText
                    anchors.verticalCenter: parent.verticalCenter
                }
                Strip {
                    pc: root
                    r: root.busRow
                    width: 30
                    height: Theme.iconSize - 6
                    windowS: 60
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }
    }

    verticalBarPill: Component {
        DW.DIcon {
            name: root.alerting ? "usb_off" : "usb"
            size: Theme.iconSize - 6
            color: root.alerting ? Theme.error : root.devRows.some(r => r.present) ? Theme.primary : Theme.surfaceVariantText
        }
    }

    popoutWidth: 600

    // ------------------------------------------------------------------ popout
    component Chip: Rectangle {
        id: chip
        property string text
        property string icon
        property color fg: Theme.surfaceText
        signal clicked
        signal rightClicked
        height: 24
        width: chipRow.implicitWidth + Theme.spacingS * 2
        radius: 12
        color: chipArea.containsMouse ? Theme.surfaceHover : "transparent"
        border.width: 1
        border.color: Theme.outlineVariant
        Row {
            id: chipRow
            anchors.centerIn: parent
            spacing: Theme.spacingXS
            DW.DIcon {
                visible: chip.icon !== ""
                name: chip.icon
                size: 14
                color: chip.fg
                anchors.verticalCenter: parent.verticalCenter
            }
            StyledText {
                text: chip.text
                color: chip.fg
                font.family: Theme.monoFontFamily
                font.pixelSize: Theme.fontSizeSmall
                anchors.verticalCenter: parent.verticalCenter
            }
        }
        MouseArea {
            id: chipArea
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            cursorShape: Qt.PointingHandCursor
            onClicked: m => m.button === Qt.RightButton ? chip.rightClicked() : chip.clicked()
        }
    }

    component Section: StyledText {
        font.pixelSize: Theme.fontSizeMedium
        font.weight: Font.Bold
        color: Theme.primary
        topPadding: Theme.spacingXS
    }

    // Port history: connected spans as bars, USB errors as red ticks, right edge = now.
    component Strip: Canvas {
        id: strip
        required property var pc
        property var r: null
        property real windowS: 60
        property real barH: height * 0.55
        onRChanged: requestPaint()
        onWidthChanged: requestPaint()
        onPaint: pc.paintStrip(getContext("2d"), width, height, r, windowS, barH)
        Connections {
            target: strip.pc
            function onNowChanged() {
                strip.requestPaint();
            }
        }
    }

    component Card: Rectangle {
        id: card
        required property var r
        required property var pc
        readonly property bool open: card.pc.expanded[r.port] === true
        width: parent ? parent.width : 0
        height: cardCol.implicitHeight + Theme.spacingS * 2
        radius: Theme.cornerRadius
        color: Theme.surfaceContainerHigh
        border.width: card.pc.recentError(r) || r.kind === "error" ? 1 : 0
        border.color: Theme.error
        opacity: r.present || r.kind === "error" ? 1 : 0.6

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: card.pc.toggle(card.r.port)
        }

        Column {
            id: cardCol
            x: Theme.spacingM
            y: Theme.spacingS
            width: parent.width - Theme.spacingM * 2
            spacing: Theme.spacingXS

            Item {
                width: parent.width
                height: titleRow.implicitHeight
                Row {
                    id: titleRow
                    spacing: Theme.spacingS
                    DW.DIcon {
                        name: card.pc.kindIcon(card.r)
                        size: Theme.iconSize - 4
                        color: card.pc.rowColor(card.r)
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    StyledText {
                        text: card.r.label + (card.r.mode ? "  ·  " + card.r.mode : "")
                        font.pixelSize: Theme.fontSizeMedium
                        font.weight: Font.Bold
                        color: card.pc.rowColor(card.r)
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    StyledText {
                        text: card.r.product !== card.r.label ? card.r.product : ""
                        color: Theme.surfaceVariantText
                        font.pixelSize: Theme.fontSizeSmall
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
                StyledText {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text: card.pc.status(card.r)
                    color: card.pc.rowColor(card.r)
                    font.pixelSize: Theme.fontSizeSmall
                    font.weight: Font.Medium
                }
            }

            Strip {
                pc: card.pc
                r: card.r
                width: parent.width
                height: 16
                barH: 8
                windowS: 600
            }
            Item {
                width: parent.width
                height: axisL.implicitHeight
                StyledText {
                    id: axisL
                    text: "−10 min"
                    color: Theme.surfaceVariantText
                    font.pixelSize: Theme.fontSizeSmall - 2
                }
                StyledText {
                    anchors.right: parent.right
                    text: "now"
                    color: Theme.surfaceVariantText
                    font.pixelSize: Theme.fontSizeSmall - 2
                }
            }

            StyledText {
                width: parent.width
                wrapMode: Text.WrapAnywhere
                font.family: Theme.monoFontFamily
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceVariantText
                text: ["port " + card.r.port, card.r.vid ? card.r.vid + ":" + card.r.pid : "", card.r.speed ? card.r.speed + " Mb/s" : "", card.r.drivers.join(","), card.r.serial ? "sn " + card.r.serial : ""].filter(s => s).join("  ·  ")
            }

            Flow {
                width: parent.width
                spacing: Theme.spacingXS
                visible: card.r.ttys.length + card.r.msc.length > 0
                Repeater {
                    model: card.r.ttys
                    Chip {
                        required property var modelData
                        icon: "terminal"
                        text: modelData.dev
                        onClicked: card.pc.copy(modelData.dev)
                        onRightClicked: card.pc.copy(modelData.by_id || modelData.dev)
                    }
                }
                Repeater {
                    model: card.r.msc
                    Chip {
                        required property var modelData
                        icon: "sd_card"
                        text: modelData.dev + (modelData.label ? " " + modelData.label : "")
                        onClicked: card.pc.copy(modelData.dev)
                    }
                }
            }

            StyledText {
                width: parent.width
                visible: text !== ""
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
                color: card.r.looping ? Theme.warning : Theme.surfaceText
                text: [card.r.reconnects ? "⟳ " + card.r.reconnects + " reconnect" + (card.r.reconnects > 1 ? "s" : "") + (card.r.looping ? " — LOOPING" : "") : "", card.r.prev ? "was " + card.r.prev : ""].filter(s => s).join("   ·   ")
            }

            StyledText {
                width: parent.width
                visible: !!card.r.last_error
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.error
                text: card.r.last_error ? "⚠ " + card.r.error_count + " error" + (card.r.error_count > 1 ? "s" : "") + ", last " + card.pc.age(card.pc.now - card.r.last_error.t) + " ago: " + card.r.last_error.text : ""
            }

            Row {
                visible: card.open
                spacing: Theme.spacingS
                Chip {
                    icon: "power_settings_new"
                    text: card.r.cycling ? "cycling…" : "cycle port"
                    fg: card.r.cycling ? Theme.warning : Theme.surfaceText
                    onClicked: {
                        if (!card.r.cycling)
                            card.pc.cycle(card.r.port);
                    }
                }
                StyledText {
                    text: "power is cut only on hubs that switch port power"
                    color: Theme.surfaceVariantText
                    font.pixelSize: Theme.fontSizeSmall - 1
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            Column {
                width: parent.width
                visible: card.open
                spacing: 1
                Repeater {
                    model: card.open ? card.r.history.slice().reverse() : []
                    StyledText {
                        required property var modelData
                        width: parent.width
                        wrapMode: Text.WrapAnywhere
                        font.family: Theme.monoFontFamily
                        font.pixelSize: Theme.fontSizeSmall - 1
                        color: modelData.sev === "error" ? Theme.error : modelData.sev === "warn" ? Theme.warning : modelData.src === "udev" ? Theme.primary : Theme.surfaceVariantText
                        text: card.pc.clock(modelData.t) + "  " + modelData.text
                    }
                }
                StyledText {
                    visible: card.r.history.length === 0
                    text: "no kernel messages for this port since the daemon started"
                    color: Theme.surfaceVariantText
                    font.pixelSize: Theme.fontSizeSmall
                }
            }
        }
    }

    popoutContent: Component {
        PopoutComponent {
            headerText: "USB Panopticon"
            detailsText: !root.linked ? "daemon offline — systemctl --user start usb-panopticon" : root.devRows.filter(r => r.present).length + " device(s) · " + root.probesUp + " probe(s) · " + root.errRows.length + " port(s) with errors" + (root.st.config_error ? "  ·  CONFIG ERROR: " + root.st.config_error : "")
            showCloseButton: true

            Column {
                width: parent.width
                spacing: Theme.spacingS

                Row {
                    spacing: Theme.spacingS
                    Chip {
                        icon: "delete_sweep"
                        text: "clear gone & errors"
                        visible: root.linked
                        onClicked: sock.send({ "op": "clear" })
                    }
                    Chip {
                        icon: "edit"
                        text: "config"
                        visible: root.linked
                        onClicked: Quickshell.execDetached(["xdg-open", root.st.config_path])
                    }
                    StyledText {
                        text: "click a card for more info"
                        color: Theme.surfaceVariantText
                        font.pixelSize: Theme.fontSizeSmall
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }

                DW.DFlickable {
                    width: parent.width
                    height: Math.min(560, Math.max(80, listCol.implicitHeight))
                    clip: true
                    contentHeight: listCol.implicitHeight
                    contentWidth: width

                    Column {
                        id: listCol
                        width: parent.width - Theme.spacingS
                        spacing: Theme.spacingS

                        StyledText {
                            visible: root.linked && root.rows.length === 0
                            text: "no MCU / serial devices and no USB errors"
                            color: Theme.surfaceVariantText
                        }
                        Section {
                            visible: root.devRows.length > 0
                            text: "Devices"
                        }
                        Repeater {
                            model: root.devRows
                            Card {
                                required property var modelData
                                r: modelData
                                pc: root
                            }
                        }
                        Section {
                            visible: root.errRows.length > 0
                            text: "USB errors"
                        }
                        Repeater {
                            model: root.errRows
                            Card {
                                required property var modelData
                                r: modelData
                                pc: root
                            }
                        }
                        Section {
                            visible: root.probeRows.length > 0
                            text: "Probes"
                        }
                        Repeater {
                            model: root.probeRows
                            Card {
                                required property var modelData
                                r: modelData
                                pc: root
                            }
                        }
                    }
                }
            }
        }
    }
}
