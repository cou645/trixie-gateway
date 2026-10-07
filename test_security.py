#!/usr/bin/env python3
"""Security regression tests (ISO 27001 gap list G1, G2): starts throwaway
gateways on spare ports with their own HOME, so the live one is untouched.

  /root/pyside6-venv/bin/python3 test_security.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def other_ip():
    """A non-loopback address of this machine (Tailscale first), or None."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    except OSError:
        return None
    ips = [l.split()[3].split("/")[0] for l in out.splitlines() if " lo " not in l]
    return next((i for i in ips if i.startswith("100.")), ips[0] if ips else None)


class Gateway:
    def __init__(self, config):
        self.dir = tempfile.mkdtemp()
        cfg = os.path.join(self.dir, "config.json")
        json.dump(config, open(cfg, "w"))
        self.port = free_port()
        env = dict(os.environ, HOME=self.dir, TRIXIE_GATEWAY_PRO_DEV_SKIP_LICENSE="1")
        self.proc = subprocess.Popen(
            [sys.executable, "gateway.py", "--port", str(self.port),
             "--socket", os.path.join(self.dir, "gw.sock"), "--config", cfg],
            cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if self.get("/yay/health")[0] == 200:
                return
            time.sleep(0.5)
        raise RuntimeError("gateway did not start")

    def get(self, path, host="127.0.0.1", headers=None, method="GET", body=None):
        req = urllib.request.Request(f"http://{host}:{self.port}{path}", method=method,
                                     headers=headers or {}, data=body)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)
        except OSError:
            return None, {}

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)


def main():
    gw = Gateway({})                      # fresh install: no capabilities section
    try:
        evil = {"Origin": "https://evil.example"}
        # G1: a web page on another site, through this PC's browser
        assert gw.get("/yay/pair/start", headers=evil)[0] == 403
        assert gw.get("/admin", headers=evil)[0] == 403
        assert gw.get("/admin/devices", method="POST", headers={**evil, "Content-Type": "application/json"},
                      body=b'{"whitelist":false,"allow":[]}')[0] == 403
        assert gw.get("/yay/pair/start", headers={"Origin": "null"})[0] == 403
        # DNS rebinding: a foreign name resolving to 127.0.0.1
        assert gw.get("/yay/pair/start", headers={"Host": "evil.example"})[0] == 403
        # no CORS header for anyone
        status, h = gw.get("/yay/health", headers=evil)
        assert "Access-Control-Allow-Origin" not in h
        status, h = gw.get("/yay/health", method="OPTIONS", headers=evil)
        assert "Access-Control-Allow-Origin" not in h
        # native local clients and the admin page itself still work
        assert gw.get("/yay/pair/start")[0] == 200
        assert gw.get("/admin")[0] == 200
        assert gw.get("/admin/devices", headers={"Origin": f"http://127.0.0.1:{gw.port}"})[0] == 200
        assert gw.get("/yay/pair/start", host="localhost")[0] == 200
        # G2: no capabilities section = authentication on
        assert gw.get("/yay/system")[0] == 401
    finally:
        gw.stop()

    gw = Gateway({"capabilities": {"allow_unauthenticated": True}})
    try:
        assert gw.get("/yay/system")[0] == 200            # testing from this PC
        ip = other_ip()
        if ip:                                            # never from elsewhere
            assert gw.get("/yay/system", host=ip)[0] in (401, 403), ip
        else:
            print("  (no non-loopback address: remote check skipped)")
    finally:
        gw.stop()
    print("security tests OK")


if __name__ == "__main__":
    main()
