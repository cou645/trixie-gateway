#!/root/pyside6-venv/bin/python3
"""Desktop control panel for the gateway service: start / stop / restart,
switch between the free and Pro units (they Conflict= each other), and show
the phone pairing QR (from /yay/network/gateway_qr, loopback-only).

  /root/pyside6-venv/bin/python3 gateway_manager.py
"""
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

UNITS = {"Pro": "trixie-gateway-pro.service", "Free": "trixie-gateway.service"}
BASE = "http://127.0.0.1:8772"


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


class Manager(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gateway Manager")
        self.unit = QComboBox()
        self.unit.addItems(UNITS)
        self.status = QLabel()
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.qr = QLabel("QR appears once the gateway is running")
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setMinimumSize(300, 300)
        self.qr.setStyleSheet("background:#fff;color:#000;padding:8px")
        self.qr.setAccessibleName("Pairing QR code")
        self.url = QLabel()
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.png = None  # bytes of the QR on screen (the one live code)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(500)

        top = QHBoxLayout()
        top.addWidget(QLabel("Service:"))
        top.addWidget(self.unit)
        for text, fn in (("Start", lambda: self.ctl("start")),
                         ("Stop", lambda: self.ctl("stop")),
                         ("Restart", lambda: self.ctl("restart")),
                         ("New QR code", self.load_qr),
                         ("Save QR…", self.save_qr),
                         ("Copy QR", self.copy_qr),
                         ("Open admin page", lambda: webbrowser.open(BASE + "/admin"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch()

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.status)
        lay.addWidget(self.qr)
        lay.addWidget(self.url, alignment=Qt.AlignCenter)
        hint = QLabel("Scan with TrXi-Ctrl → Settings → QR-scanner icon. Phone not "
                      "here? Save or copy the QR, email it, and on the phone tap the "
                      "image icon in the scanner. The code works once and expires "
                      "after 10 minutes; pressing New QR code cancels the old one.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addWidget(self.log, 1)

        # preselect whichever unit is running
        for name, unit in UNITS.items():
            if sh("systemctl", "is-active", unit)[1] == "active":
                self.unit.setCurrentText(name)
        self.unit.currentTextChanged.connect(self.refresh)
        self.timer = QTimer(self, interval=3000, timeout=self.refresh)
        self.timer.start()
        self.refresh()
        self.load_qr()

    def current(self):
        return UNITS[self.unit.currentText()]

    def ctl(self, action):
        # Start/restart pulls in the chosen unit; Conflicts= stops the other.
        rc, out = sh("systemctl", action, self.current())
        if rc:
            self.status.setText(f"systemctl {action} failed: {out}")
        self.refresh()
        if action != "stop":
            QTimer.singleShot(2500, self.load_qr)  # give aiohttp time to bind

    def refresh(self):
        states = {n: sh("systemctl", "is-active", u)[1] for n, u in UNITS.items()}
        self.status.setText("   ".join(f"{n}: <b>{s}</b>" for n, s in states.items()))
        self.log.setPlainText(sh("journalctl", "-u", self.current(), "-n", "200",
                                 "--no-pager", "-o", "short-iso")[1])
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def load_qr(self):
        # Each fetch mints a fresh one-time pairing code, so only on demand.
        try:
            with urllib.request.urlopen(BASE + "/yay/network/gateway_qr", timeout=5) as r:
                self.png = r.read()
                pix = QPixmap()
                pix.loadFromData(self.png)
                self.qr.setPixmap(pix.scaled(280, 280, Qt.KeepAspectRatio))
                self.url.setText("%s    code: %s" % (r.headers.get("X-Gateway-URL", ""),
                                                   r.headers.get("X-Pairing-Code", "")))
        except urllib.error.HTTPError as e:
            self.png = None
            self.qr.setText(e.read().decode(errors="replace"))
            self.url.clear()
        except OSError as e:
            self.png = None
            self.qr.setText(f"Gateway not reachable on {BASE}\n({e})")
            self.url.clear()

    # Save/copy the QR already shown: fetching again would cancel its code.
    def save_qr(self):
        if not self.png:
            return self.status.setText("No QR to save — is the gateway running?")
        path, _ = QFileDialog.getSaveFileName(self, "Save pairing QR",
                                              "gateway-pairing-qr.png", "PNG (*.png)")
        if path:
            with open(path, "wb") as f:
                f.write(self.png)
            self.status.setText(f"Saved {path} (valid 10 min, one use)")

    def copy_qr(self):
        if not self.png:
            return self.status.setText("No QR to copy — is the gateway running?")
        QApplication.clipboard().setPixmap(self.qr.pixmap())
        self.status.setText("QR copied: paste it into an email (valid 10 min, one use)")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Manager()
    w.resize(760, 760)
    w.show()
    sys.exit(app.exec())
