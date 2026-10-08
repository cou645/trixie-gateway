#!/usr/bin/env python3
"""Security regression tests (ISO 27001 gap list G1-G4, G7-G9): starts throwaway
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


def my_ips():
    """(tailscale ip or None, private LAN ip or None) of this machine."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    except OSError:
        return None, None
    ips = [l.split()[3].split("/")[0] for l in out.splitlines() if " lo " not in l]
    ts = next((i for i in ips if i.startswith("100.")), None)
    lan = next((i for i in ips if i.startswith(("10.", "192.168.", "172."))), None)
    return ts, lan


def other_ip():
    ts, lan = my_ips()
    return ts or lan


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

    def call(self, path, body=None, token=None, host="127.0.0.1"):
        """POST JSON, return (status, decoded body)."""
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(f"http://{host}:{self.port}{path}", method="POST",
                                     headers=h, data=json.dumps(body or {}).encode())
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def read(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return json.loads(r.read())

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


def check_needs_root():
    """G6: root-only routes answer 403 when the gateway isn't root."""
    sys.path.insert(0, HERE)
    sys.argv = sys.argv[:1]
    import gateway
    from aiohttp.test_utils import make_mocked_request
    real = os.geteuid
    try:
        os.geteuid = lambda: 1000
        assert gateway._needs_root(make_mocked_request("GET", "/yay/firewall/rules"))
        assert gateway._needs_root(make_mocked_request("PUT", "/yay/layers/x"))
        assert not gateway._needs_root(make_mocked_request("GET", "/yay/system"))
        os.geteuid = lambda: 0
        assert not gateway._needs_root(make_mocked_request("GET", "/yay/firewall/rules"))
    finally:
        os.geteuid = real


def main():
    check_needs_root()
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
    ts, lan = my_ips()
    gw = Gateway({})
    try:
        # G3: LAN addresses refused unless network.allow_lan
        if lan:
            assert gw.get("/yay/health", host=lan)[0] == 403, lan
        # G9: 5 wrong codes, then locked
        for _ in range(5):
            assert gw.call("/yay/pair", {"pairing_code": "nope"})[0] == 403
        assert gw.call("/yay/pair", {"pairing_code": "nope"})[0] == 429
    finally:
        gw.stop()

    gw = Gateway({"network": {"allow_lan": True}})
    try:
        if lan:
            # G5: home network only over HTTPS, with the pinned certificate
            assert gw.get("/yay/health", host=lan)[0] == 403
            import ssl, hashlib, http.client
            fp = gw.read("/yay/health")["tls_sha256"]
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            c = http.client.HTTPSConnection(lan, gw.port + 1, context=ctx, timeout=10)
            c.request("GET", "/yay/health")
            assert c.getresponse().status == 200
            der = c.sock.getpeercert(binary_form=True)
            assert hashlib.sha256(der).hexdigest() == fp and len(fp) == 64, fp
            c.close()
        code = gw.read("/yay/pair/start")["pairing_code"]
        st, r = gw.call("/yay/pair", {"pairing_code": code, "device_name": "TestPhone"},
                        host=ts or "127.0.0.1")
        assert st == 200, (st, r)
        tok = r["token"]
        # whitelist switched on by the first pairing, with the new device in it
        devs = gw.read("/admin/devices")
        assert devs["whitelist"] and devs["allow"], devs
        # G4: new device has no terminal until granted on /admin
        st, r = gw.call("/yay/terminal/exec", {"cmd": "echo hi"}, tok)
        assert st == 200 and not r["ok"] and "terminal is off" in r["stderr"].lower(), r
        jti = next(t["jti"] for t in gw.read("/admin/tokens")["tokens"] if t["sub"] == "TestPhone")
        assert gw.call("/admin/tokens", {"jti": jti, "terminal": True})[0] == 200
        st, r = gw.call("/yay/terminal/exec", {"cmd": "echo hi"}, tok)
        assert r["ok"] and r["stdout"].strip() == "hi", r
        # renewal: same device and permissions, old token dead, no scope upgrade
        st, r = gw.call("/yay/capability/issue", {"scope": "admin", "subject": "evil", "ttl": 3600}, tok)
        assert st == 200, r
        new = r["token"]
        assert gw.call("/yay/terminal/exec", {"cmd": "echo hi"}, tok)[0] == 401
        assert gw.call("/yay/terminal/exec", {"cmd": "echo hi"}, new)[1]["ok"]
        assert not any(t["sub"] == "evil" for t in gw.read("/admin/tokens")["tokens"])
        # revoke
        jti = next(t["jti"] for t in gw.read("/admin/tokens")["tokens"]
                   if t["sub"] == "TestPhone" and not t["revoked"])
        assert gw.call("/admin/tokens", {"jti": jti, "revoked": True})[0] == 200
        assert gw.call("/yay/terminal/exec", {"cmd": "echo hi"}, new)[0] == 401
        # G8: clear clipboard history
        assert gw.call("/admin/clipboard/clear")[0] == 200
        # G7: journal is owner-only, hash-chained, and logged the refusals
        jpath = os.path.join(gw.dir, ".config", "trixie-gateway", "journal.jsonl")
        assert oct(os.stat(jpath).st_mode)[-3:] == "600"
        events = [json.loads(l) for l in open(jpath)]
        types = {e["type"] for e in events}
        assert {"device_paired", "auth_refused", "terminal_permission",
                "device_revoked", "whitelist_change", "terminal_exec"} <= types, types
        sys.path.insert(0, HERE)
        from semantic_journal import SemanticJournal
        assert SemanticJournal({"path": jpath}).verify()[0]
    finally:
        gw.stop()
    print("security tests OK")


if __name__ == "__main__":
    main()
