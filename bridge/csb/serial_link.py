"""Serial transport to the LilyGo display, plus logo streaming."""

import json
import os
import time


class SerialLink:
    VID_PID = [(0x303A, 0x1001)]   # Espressif native USB

    def __init__(self, port, baud):
        self.port_cfg = port
        self.baud = baud
        self.ser = None
        self.on_line = None    # callback(line) for device-initiated commands

    def _detect(self):
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        for p in ports:
            if (p.vid, p.pid) in self.VID_PID:
                return p.device
        # fall back to anything that looks like a USB serial device
        # (avoids grabbing Bluetooth/debug ports on macOS)
        usb = [p for p in ports if any(k in p.device for k in
               ("usbmodem", "usbserial", "ttyACM", "ttyUSB", "COM"))]
        if len(usb) == 1:
            return usb[0].device
        return ports[0].device if len(ports) == 1 else None

    def send(self, text):
        import serial
        if self.ser is None:
            port = self.port_cfg or self._detect()
            if not port:
                return False
            try:
                self.ser = serial.Serial(port, self.baud, timeout=0, write_timeout=2)
                print(f"[serial] connected on {port}")
            except Exception as e:
                print(f"[serial] open failed on {port}: {e}")
                self.ser = None
                return False
        try:
            self.ser.write(text.encode("utf-8"))
            try:                       # echo anything the device says
                n = self.ser.in_waiting
                if n:
                    for ln in self.ser.read(n).decode(errors="replace").splitlines():
                        ln = ln.strip()
                        if not ln:
                            continue
                        print(f"[device] {ln}")
                        if self.on_line:
                            try:       # device-initiated commands (focus etc.)
                                self.on_line(ln)
                            except Exception as e:
                                print(f"[serial] on_line handler failed: {e}")
            except Exception:
                pass
            return True
        except Exception as e:
            print(f"[serial] lost connection: {e}")
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            return False


def send_logo(link, path):
    """Stream logo.bin (48x48 RGB565) to the display in chunks."""
    if not (link and os.path.exists(path)):
        return
    data = open(path, "rb").read()
    if len(data) != 48 * 48 * 2:
        print(f"[logo] logo.bin has wrong size ({len(data)}), run set_logo again")
        return
    chunk = 768
    for off in range(0, len(data), chunk):
        part = data[off:off + chunk]
        pkt = {"t": "lg", "off": off, "px": part.hex(),
               "last": off + chunk >= len(data)}
        link.send(json.dumps(pkt, separators=(",", ":")) + "\n")
        time.sleep(0.05)
    print("[logo] sent to display")
