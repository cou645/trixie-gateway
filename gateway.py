#!/root/pyside6-venv/bin/python3
# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""
trixie-gateway — AI gateway daemon (Debian Trixie / X11 / systemd edition)

The PC-side counterpart to the yayos-apk phone app. Ported from fd64-gateway
(the Fatdog64 fork — kept alongside as the frozen reference for the
YaYOS-gateway-v2.sfs bundle):
  - Init    : systemd unit (trixie-gateway.service), not SysV rc.d
  - Python  : /root/pyside6-venv (system python3 3.13 has no aiohttp)
  - Wi-Fi   : interface auto-detected (was hard-coded wlan2)
  - Paths   : ~/.config/trixie-gateway/config.json (legacy ~/.yayos/* still read);
              data root resolves /mnt/sda2 -> /aufs/devbase
  - Layers  : still AUFS branches on this box (7 aufs mounts)
  - Audio   : amixer + pactl/wpctl

  - OpenAI-compatible /v1/chat/completions (routes to Anthropic/OpenAI/local)
  - system info, layer management, semantic journal
  - Listens on Unix socket (local tools) + TCP (phone/LAN/Tailscale)

Usage:
  python3 gateway.py [--port 8772] [--socket /run/trixie-gateway.sock]
                     [--config ~/.config/trixie-gateway/config.json] [--debug]
"""

import asyncio
import json
import logging
import ipaddress
import os
import platform
import re
import signal
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.web_middlewares import normalize_path_middleware

import hmac
import secrets
import time

import platforms
from capability_broker import CapabilityBroker
from semantic_journal import SemanticJournal
from providers.router import ProviderRouter
from capabilities import system_info, layer_control, brightness, audio, bluetooth, chat_history, desktop_input, lockscreen, firewall, media, personal_manager, terminal, webrtc_screen, wm, apps, clipboard, clip_rules, app_updates

# Pro edition: license_client.py / license_fingerprint.py live in the private
# trixie-gateway-pro repo and are copied next to this file for Pro builds.
# Without them this is the free edition (identical features today).
try:
    import license_client
except ImportError:
    license_client = None

LOG = logging.getLogger("trixie-gateway")


def _is_loopback(req: web.Request) -> bool:
    peer = req.remote
    return not peer or peer in ("127.0.0.1", "::1")

# Routes reachable with no capability token. Keep this list short.
_PUBLIC_ROUTES = {
    ("GET",  "/yay/health"),
    ("GET",  "/yay/network/gateway_qr"),
    ("GET",  "/yay/pair/start"),
    ("POST", "/yay/pair"),
}

ADMIN_HOME = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gateway — %(host)s</title>
<style>
 body{font:15px/1.45 system-ui,sans-serif;margin:0 auto;max-width:900px;padding:16px;background:#111;color:#eee}
 h1{font-size:1.3em} h2{font-size:1.1em;margin-top:1.4em}
 .row{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}
 .qr{background:#fff;padding:12px;border-radius:8px} .qr img{display:block;width:260px;height:260px}
 table{border-collapse:collapse;width:100%%} th,td{border:1px solid #444;padding:5px 8px;text-align:left} th{background:#222}
 a{color:#7dd3fc} button{background:#2563eb;color:#fff;border:0;padding:7px 14px;border-radius:4px;font-size:1em;cursor:pointer}
 :focus-visible{outline:3px solid #facc15;outline-offset:2px} .muted{color:#aaa} code{background:#222;padding:1px 5px;border-radius:3px}
 button.small{padding:3px 9px;font-size:.9em}
</style></head><body>
<h1>Gateway on %(host)s</h1>
<p class="muted">This page only opens on this PC. Phones reach the gateway on port %(port)s over Tailscale.</p>
<h2>Pair a phone</h2>
<div class="row">
 <div class="qr"><img id="qr" alt="Pairing QR code: the gateway address and a one-time pairing code"></div>
 <div>
  <ol>
   <li>On the phone, open <b>TrXi-Ctrl → Settings</b>.</li>
   <li>Tap the <b>QR-scanner icon</b> beside the gateway address and point the camera at this code.</li>
   <li>The phone fills in the gateway address and pairs.</li>
  </ol>
  <p>No camera (Windows/Linux app)? In <b>Settings</b> enter the gateway address and this code:</p>
  <p role="status" aria-live="polite" style="font-size:1.3em"><code id="paircode">…</code> &nbsp;<span id="gwurl" class="muted"></span></p>
  <p class="muted">The code works once and expires after 10 minutes.</p>
  <button onclick="loadQr()">New code</button>
  <script>
  async function loadQr() {
    const r = await fetch('/yay/network/gateway_qr?' + Date.now());
    if (!r.ok) { document.getElementById('paircode').textContent = (await r.json()).error; return; }
    document.getElementById('qr').src = URL.createObjectURL(await r.blob());
    document.getElementById('paircode').textContent = r.headers.get('X-Pairing-Code');
    document.getElementById('gwurl').textContent = r.headers.get('X-Gateway-URL');
  }
  loadQr();
  </script>
  <p class="muted">No QR? Check Tailscale is up: <code>ip -4 addr show tailscale0</code> must show a 100.x address
  (if not: <code>systemctl restart tailscaled</code>).</p>
 </div>
</div>
<h2>Paired devices</h2>
<p>The gateway runs as <b>%(account)s</b>. %(account_note)s</p>
<p class="muted">Each phone or computer that paired. A device unused for 90 days must pair again.
<b>Terminal</b> lets that device run commands on this PC as %(account)s; new devices start with it off.
<b>Revoke</b> cuts a lost or sold device off at once.</p>
<table><thead><tr><th>Device</th><th>Paired</th><th>Last used</th><th>Status</th><th>Terminal</th><th></th></tr></thead>
<tbody id="toks"><tr><td colspan=6 class="muted">Loading…</td></tr></tbody></table>
<script>
async function tokAction(jti, change) {
  await fetch('/admin/tokens', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(Object.assign({jti: jti}, change))});
  loadToks();
}
async function loadToks() {
  const d = await (await fetch('/admin/tokens')).json();
  const day = t => t ? new Date(t * 1000).toLocaleDateString() : '';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const rows = d.tokens.sort((a, b) => (b.last_used || 0) - (a.last_used || 0)).map(t => {
    const idle = d.now - (t.last_used || 0) > d.idle_limit;
    const st = t.revoked ? '<b style="color:#f87171">revoked</b>' : idle ? 'expired (unused 90 days)' : '<b style="color:#4ade80">active</b>';
    const live = !t.revoked && !idle;
    return '<tr><td>' + esc(t.sub) + '</td><td>' + day(t.issued) + '</td><td>' + day(t.last_used) + '</td><td>' + st + '</td>' +
      '<td><label><input type="checkbox" data-term="' + esc(t.jti) + '" ' + (t.terminal ? 'checked ' : '') + (live ? '' : 'disabled ') +
      'aria-label="Terminal for ' + esc(t.sub) + '"> allowed</label></td>' +
      '<td>' + (live ? '<button class="small" style="background:#b91c1c" data-revoke="' + esc(t.jti) + '" data-name="' + esc(t.sub) + '">Revoke</button>' : '') + '</td></tr>';
  });
  document.getElementById('toks').innerHTML = rows.join('') || '<tr><td colspan=6 class="muted">No device has paired yet.</td></tr>';
  document.querySelectorAll('[data-term]').forEach(c => c.onchange = () => tokAction(c.dataset.term, {terminal: c.checked}));
  document.querySelectorAll('[data-revoke]').forEach(b => b.onclick = () => {
    if (confirm('Revoke ' + b.dataset.name + '? It will have to pair again.')) tokAction(b.dataset.revoke, {revoked: true});
  });
}
loadToks();
</script>
<h2>Devices</h2>
<p class="muted">Every phone that has reached the gateway: on Tailscale by its Tailscale name (the MAC
isn't visible across Tailscale), on the home network by its MAC. <b>Online</b> = seen in the last 2 minutes.</p>
<p><label><input type="checkbox" id="wl"> Only allow ticked devices (whitelist). Everything else is refused,
pairing included, and listed here as <b>blocked</b> so you can tick it. This PC is always allowed.</label></p>
<table><thead><tr><th>Allowed</th><th>Device</th><th>Connection</th><th>Address</th><th>Paired as</th><th>Status</th></tr></thead>
<tbody id="devs"><tr><td colspan=6 class="muted">Loading…</td></tr></tbody></table>
<p><label>Add by MAC or key: <input id="addkey" placeholder="aa:bb:cc:dd:ee:ff or ts:name"
 style="background:#000;color:#fff;border:1px solid #888;padding:4px"></label>
 <button id="add">Add</button> <button id="savedev">Save</button> <span id="devmsg" role="status" aria-live="polite"></span></p>
<p class="muted">Turning the whitelist on with nothing ticked locks every phone out. Android uses a separate
random MAC per Wi-Fi network, so a phone can show a different MAC on another network.</p>
<script>
let dv = null, dirty = false;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function drawDevs() {
  const keys = new Set([...dv.seen.map(s => s.key), ...dv.allow]);
  const by = Object.fromEntries(dv.seen.map(s => [s.key, s]));
  const rows = [...keys].sort((a, b) => ((by[b] || {}).last || 0) - ((by[a] || {}).last || 0));
  document.getElementById('wl').checked = dv.whitelist;
  document.getElementById('devs').innerHTML = rows.map(k => {
    const s = by[k] || {}, age = s.last ? dv.now - s.last : Infinity;
    const st = s.blocked && dv.whitelist && !dv.allow.includes(k) ? '<b style="color:#f87171">blocked</b>'
      : age < 120 ? '<b style="color:#4ade80">online</b>'
      : s.last ? 'last seen ' + new Date(s.last * 1000).toLocaleString() : 'never seen';
    return `<tr><td><input type="checkbox" data-k="${esc(k)}" ${dv.allow.includes(k) ? 'checked' : ''}
      aria-label="Allow ${esc(s.label || k)}"></td><td>${esc(s.label || '')}</td><td>${esc(k)}</td>
      <td>${esc(s.ip)}</td><td>${esc(s.name)}</td><td>${st}</td></tr>`;
  }).join('') || '<tr><td colspan=6 class="muted">No phone has connected yet.</td></tr>';
  document.querySelectorAll('#devs input').forEach(c => c.onchange = () => {
    dirty = true; dv.allow = dv.allow.filter(x => x !== c.dataset.k).concat(c.checked ? [c.dataset.k] : []); });
}
async function loadDevs() { if (!dirty) { dv = await (await fetch('/admin/devices')).json(); drawDevs(); } }
document.getElementById('wl').onchange = e => { dirty = true; dv.whitelist = e.target.checked; drawDevs(); };
document.getElementById('add').onclick = () => {
  let k = document.getElementById('addkey').value.trim().toLowerCase();
  if (/^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$/.test(k)) k = 'mac:' + k.replace(/-/g, ':');
  else if (!/^(mac|ts|ip|name):./.test(k)) { document.getElementById('devmsg').textContent = 'Enter a MAC (aa:bb:cc:dd:ee:ff) or a key like ts:pixel-8'; return; }
  if (!dv.allow.includes(k)) dv.allow.push(k);
  dirty = true; document.getElementById('addkey').value = ''; drawDevs();
};
document.getElementById('savedev').onclick = async () => {
  const r = await fetch('/admin/devices', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({whitelist: dv.whitelist, allow: dv.allow})});
  document.getElementById('devmsg').textContent = r.ok ? 'Saved.' : 'Not saved: ' + r.status;
  if (r.ok) { dirty = false; loadDevs(); }
};
loadDevs(); setInterval(loadDevs, 5000);
</script>
<h2>Clipboard sharing</h2>
<p>%(clips)s items in the shared history (unlocked items expire after %(expire)s h) · %(named)s named devices · <a href="/admin/clipboard">who receives from whom →</a>
 <button class="small" onclick="if (confirm('Clear every unlocked item from the shared history?')) fetch('/admin/clipboard/clear', {method: 'POST'}).then(() => location.reload())">Clear history</button></p>
<h2>App updates</h2>
<table><thead><tr><th>App</th><th>Version</th><th>Published</th></tr></thead><tbody>%(apps)s</tbody></table>
<p class="muted">Phones install these from TrXi-Ctrl → Updates. Publish a new build with
<code>python3 capabilities/app_updates.py publish --flutter &lt;project&gt; --package &lt;id&gt; --name &lt;name&gt;</code></p>
</body></html>"""

DEFAULT_SOCKET = "/run/trixie-gateway.sock"
DEFAULT_PORT   = 8772
DEFAULT_CONFIG = Path.home() / ".config" / "trixie-gateway" / "config.json"
# older locations still honoured on load, in order
LEGACY_CONFIGS = [
    Path.home() / ".yayos" / "fd64-gateway.json",
    Path.home() / ".yayos" / "gateway.json",
]
DATA_UUID      = "e844b1ba-7aa7-4c30-9542-d8114ee9aa6a"


def _detect_wifi_iface() -> str:
    """First wireless interface that isn't loopback/tailscale, else a sane default."""
    if os.environ.get("WIFI_IFACE"):
        return os.environ["WIFI_IFACE"]
    try:
        for entry in sorted(os.listdir("/sys/class/net")):
            if entry in ("lo",) or entry.startswith(("tailscale", "docker", "veth", "br-")):
                continue
            if os.path.isdir(f"/sys/class/net/{entry}/wireless") or \
               os.path.exists(f"/sys/class/net/{entry}/phy80211"):
                return entry
    except OSError:
        pass
    return "wlan0"


WIFI_IFACE = _detect_wifi_iface()


# Route prefix -> feature name in platforms.FEATURES. Anything not listed is
# assumed portable (chat, journal, providers, capability tokens, health).
_FEATURE_ROUTES = {
    "/yay/layers":      "layers",
    "/yay/firewall":    "firewall",
    "/yay/bluetooth":   "bluetooth",
    "/yay/brightness":  "brightness",
    "/yay/network":     "network",
    "/yay/media":       "media",
}


# Linux routes that only work as root. Most users run the gateway as their
# normal account (safer: a device's commands only reach that account), so
# these answer a clear 403 instead of failing deep inside iptables/iwlist
# (ISO 27001 gap G6).
_ROOT_ROUTES = [(None, "/yay/firewall"), ("PUT", "/yay/layers/"), (None, "/yay/network/wifi")]


def _needs_root(req: web.Request) -> bool:
    if sys.platform != "linux" or os.geteuid() == 0:
        return False
    return any(req.path.startswith(prefix) and (method is None or req.method == method)
               for method, prefix in _ROOT_ROUTES)


@web.middleware
async def platform_guard_middleware(req: web.Request, handler):
    """Answer Linux-only routes with a clear 501 on Windows/macOS.

    Without this the handlers still run and fail deep inside a missing
    iptables/nmcli/aufs, producing a 500 and a stack trace that says nothing
    useful to whoever is holding the phone.
    """
    if _needs_root(req):
        return web.json_response(
            {"ok": False, "error": "needs administrator rights: the gateway runs as a normal "
             "user here (safer). Run it as root only if you need this feature."}, status=403)
    for prefix, feature in _FEATURE_ROUTES.items():
        if req.path.startswith(prefix) and not platforms.supported(feature):
            return web.json_response(platforms.unsupported_result(feature), status=501)
    return await handler(req)


_TS_NET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")   # Tailscale IPv6

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _browser_foreign(req: web.Request) -> bool:
    """A loopback request that a web page on another site made through this
    PC's browser (or a DNS-rebinding name pointing at 127.0.0.1). Loopback is
    trusted for /admin, pairing and the QR, so a page on any website could
    otherwise pair itself and run commands. Native clients (the apps, curl,
    Gateway Manager) send no Origin and use a localhost Host."""
    host = req.host.rsplit(":", 1)[0] if not req.host.startswith("[") \
        else req.host.split("]")[0] + "]"
    if host not in _LOCAL_HOSTS:
        return True
    origin = req.headers.get("Origin")
    return origin is not None and origin != f"{req.scheme}://{req.host}"


class TrixieGateway:
    def __init__(self, config_path: Path, wifi_iface: str = WIFI_IFACE):
        self.wifi_iface = wifi_iface
        self.config_path = Path(config_path)
        self.config = self._load_config(self.config_path)
        self.broker  = CapabilityBroker(self.config.get("capabilities", {}))
        self._pairing_code    = None
        self._pairing_expires = 0.0
        self._pairing_used    = True
        self._pair_failures: list[float] = []   # wrong codes in the last minute
        self._refused_at: dict = {}             # (ip, path) -> last journal time
        # Shared clipboard history (capabilities/clipboard.History): every
        # device's pushes and the PC's own copies, newest first
        self._clip = clipboard.History(max_age=int(
            self.config.get("clipboard", {}).get("expire_hours", 24) * 3600))
        self._rules = clip_rules.Rules()   # who receives whose shares
        self._pc_seen = None          # last PC clipboard text looked at
        self.journal = SemanticJournal(self.config.get("journal", {}))
        self.router  = ProviderRouter(self.config.get("providers", {}))
        self.sda2    = self._resolve_sda2()
        self.app     = self._build_app()
        self.journal.append("boot", "trixie-gateway")

    def _load_config(self, path: Path) -> dict:
        for p in [path, *LEGACY_CONFIGS]:
            if p.exists():
                if p != path:
                    LOG.info("using legacy config %s", p)
                    self.config_path = p
                return json.loads(p.read_text())
        LOG.warning("no config found (%s) — using defaults", path)
        return {}

    def _resolve_sda2(self) -> Path:
        env = os.environ.get("SDA2")
        if env and Path(env).is_dir():
            return Path(env)
        # Fatdog64-derived distros (this box included) stack under /aufs;
        # standard Puppy Linux stacks under /initrd instead — no /aufs
        # directory exists there at all. Try both as cross-distro fallbacks
        # when the raw /mnt/sda2 mount isn't present.
        for candidate in ["/mnt/sda2", "/mnt/data", "/aufs/devbase", "/initrd/devbase"]:
            if Path(candidate).is_dir():
                return Path(candidate)
        return Path("/mnt/sda2")

    def _build_app(self) -> web.Application:
        @web.middleware
        async def auth_middleware(req: web.Request, handler):
            resp = await self._authorise(req)
            if resp is not None:
                self._log_refusal(req, resp.status)
                return resp
            return await handler(req)

        app = web.Application(middlewares=[auth_middleware,
                                           platform_guard_middleware,
                                           normalize_path_middleware()])
        self._routes(app)
        return app

    def _network_allowed(self, ip: str) -> bool:
        """Tailscale, this PC, and -- only with network.allow_lan -- private LAN
        addresses. The listener binds 0.0.0.0, so without this a gateway on a
        cafe's Wi-Fi answered everyone there (ISO 27001 gap G3)."""
        if not ip or clip_rules.is_local(ip):
            return True
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if addr in clip_rules._TS_NET or (addr.version == 6 and addr in _TS_NET6):
            return True
        allow_lan = self.config.get("network", {}).get("allow_lan", False)
        return bool(allow_lan and (addr.is_private or addr.is_link_local))

    def _log_refusal(self, req: web.Request, status: int):
        """Refused requests go in the journal, once a minute per address+path."""
        key, now = (req.remote, req.path), time.time()
        if now - self._refused_at.get(key, 0) < 60:
            return
        self._refused_at[key] = now
        if len(self._refused_at) > 1000:
            self._refused_at.clear()
        self.journal.append("auth_refused", req.path,
                            {"remote": req.remote, "status": status, "method": req.method})

    async def _authorise(self, req: web.Request):
        """None = let it through, else the refusal response."""
        if not self._network_allowed(req.remote):
            return web.json_response(
                {"error": "this gateway only answers over Tailscale "
                          "(LAN access: network.allow_lan in config.json)"}, status=403)
        if req.remote and clip_rules.is_local(req.remote) and _browser_foreign(req):
            return web.json_response(
                {"error": "cross-site request refused"}, status=403)
        if not clip_rules.is_local(req.remote):   # device list + whitelist
            tok = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            name = (tok and self.broker.subject(tok)) or "unknown"
            # pairing and the health ping stay open: the one-time code shown
            # on this PC is the owner's consent, and pairing adds the device
            if not self._rules.admit(req.remote, name) and \
                    (req.method, req.path) not in _PUBLIC_ROUTES:
                return web.json_response(
                    {"error": "this device is not on the gateway's whitelist "
                              "(allow it on the PC: /admin)"}, status=403)
        if req.path.startswith("/dl/"):         # app share links: the token
            return None                         # in the URL is the key
        if req.path == "/admin" or req.path.startswith("/admin/"):  # this PC only
            if not clip_rules.is_local(req.remote):
                return web.json_response({"error": "admin pages are local only"}, status=403)
            return None
        if (req.method, req.path) in _PUBLIC_ROUTES or not req.remote:
            return None
        token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        # allow_unauthenticated (testing) never reaches beyond this PC
        if not (token or clip_rules.is_local(req.remote)) or \
                not self.broker.validate(token, scope="device"):
            return web.json_response(
                {"error": "missing, revoked or expired device token — pair again via the QR"},
                status=401)
        req["device"] = self.broker.subject(token)
        return None

    def _routes(self, app: web.Application):
        # OpenAI-compatible
        app.router.add_post("/v1/chat/completions", self._chat_completions)
        app.router.add_get( "/v1/models",           self._list_models)
        # YaYOS system
        app.router.add_get( "/yay/health",          self._health)
        app.router.add_get( "/yay/system",          self._system_info)
        app.router.add_get( "/yay/capabilities",    self._capabilities)
        # Layers
        app.router.add_get( "/yay/layers",          self._get_layers)
        app.router.add_put( "/yay/layers/{name}",   self._set_layer)
        # Journal
        app.router.add_get( "/yay/journal",         self._query_journal)
        app.router.add_post("/yay/journal",         self._append_journal)
        # Providers / API key config
        app.router.add_get( "/yay/providers",        self._get_providers)
        app.router.add_put( "/yay/providers",        self._set_providers)
        # Capability
        app.router.add_post("/yay/capability/issue", self._issue_capability)
        app.router.add_post("/yay/capability/check", self._check_capability)
        app.router.add_get( "/yay/pair/start",       self._pair_start)
        app.router.add_post("/yay/pair",             self._pair_device)
        # Brightness
        app.router.add_get( "/yay/brightness",              self._get_brightness)
        app.router.add_put( "/yay/brightness/screen",       self._set_brightness_screen)
        app.router.add_put( "/yay/brightness/kbd",          self._set_brightness_kbd)
        # Audio
        app.router.add_get( "/yay/audio",                   self._get_audio)
        app.router.add_get( "/yay/audio/sinks",             self._list_sinks)
        app.router.add_put( "/yay/audio/volume",            self._set_volume)
        app.router.add_put( "/yay/audio/mute",              self._set_mute)
        # Network
        app.router.add_get( "/yay/network/status",          self._net_status)
        app.router.add_get( "/yay/network/interfaces",      self._net_interfaces)
        app.router.add_get( "/yay/network/tailscale_peers", self._net_tailscale_peers)
        app.router.add_get( "/yay/network/gateway_qr",      self._qr_gateway_url)
        app.router.add_get( "/yay/network/wifi/scan",       self._wifi_scan)
        app.router.add_post("/yay/network/wifi/connect",    self._wifi_connect)
        app.router.add_post("/yay/network/wifi/disconnect", self._wifi_disconnect)
        # Media
        app.router.add_get( "/yay/media/sessions",      self._media_sessions)
        app.router.add_get( "/yay/media/status",        self._media_status)
        app.router.add_post("/yay/media/play",           self._media_play)
        app.router.add_post("/yay/media/pause",          self._media_pause)
        app.router.add_post("/yay/media/stop",           self._media_stop)
        app.router.add_put( "/yay/media/volume",         self._media_volume)
        app.router.add_put( "/yay/media/seek",           self._media_seek)
        app.router.add_post("/yay/media/open",           self._media_open)
        app.router.add_get(   "/yay/media/recent",         self._media_recent)
        app.router.add_delete("/yay/media/recent",         self._media_clear_recent)
        app.router.add_get(   "/yay/media/browse",         self._media_browse)
        app.router.add_post("/yay/media/source",         self._media_source)
        app.router.add_delete("/yay/media/session",      self._media_kill)
        # PersonalManager (alarms, timetable, rsync, cron) — via pm_store
        app.router.add_get(   "/yay/pm/items",                 self._pm_list)
        app.router.add_post(  "/yay/pm/items/{collection}",    self._pm_add)
        app.router.add_put(   "/yay/pm/items/{collection}/{id}", self._pm_update)
        app.router.add_delete("/yay/pm/items/{collection}/{id}", self._pm_remove)
        app.router.add_post(  "/yay/pm/apply",                 self._pm_apply)
        app.router.add_post(  "/yay/pm/cron/enabled",          self._pm_cron_enabled)
        app.router.add_get(   "/yay/pm/format",                self._pm_format)
        # Window manager
        app.router.add_get( "/yay/wm/windows",           self._wm_list)
        app.router.add_get( "/yay/wm/active",            self._wm_active)
        app.router.add_get( "/yay/wm/desktops",          self._wm_desktops)
        app.router.add_put( "/yay/wm/desktop",           self._wm_set_desktop)
        app.router.add_post("/yay/wm/focus",             self._wm_focus)
        app.router.add_post("/yay/wm/close",             self._wm_close)
        app.router.add_post("/yay/wm/minimize",          self._wm_minimize)
        app.router.add_post("/yay/wm/maximize",          self._wm_maximize)
        app.router.add_post("/yay/wm/restore",           self._wm_restore)
        app.router.add_post("/yay/wm/fullscreen",        self._wm_fullscreen)
        app.router.add_put( "/yay/wm/geometry",          self._wm_geometry)
        app.router.add_post("/yay/wm/tile",              self._wm_tile)
        # Lockscreen
        app.router.add_get( "/yay/lock/status",             self._lock_status)
        app.router.add_post("/yay/lock/lock",               self._lock_lock)
        app.router.add_post("/yay/lock/unlock",             self._lock_unlock)
        # Clipboard
        app.router.add_get( "/yay/clipboard",               self._clipboard_get)
        app.router.add_put( "/yay/clipboard",               self._clipboard_set)
        app.router.add_post("/yay/clipboard/mute",          self._clipboard_mute)
        app.router.add_delete("/yay/clipboard/{id}",        self._clipboard_delete)
        app.router.add_post("/yay/clipboard/{id}/lock",     self._clipboard_lock)
        app.router.add_post("/yay/clipboard/{id}/pc",       self._clipboard_to_pc)
        app.router.add_post("/yay/clipboard/{id}/save",     self._clipboard_save)
        # Phone app updates (capabilities/app_updates.py)
        app.router.add_get( "/yay/updates",                 self._updates_list)
        app.router.add_get( "/yay/updates/{package}/{abi}", self._updates_download)
        app.router.add_post("/yay/updates/{package}/share", self._updates_share)
        app.router.add_get( "/dl/{token}/{name}",           self._updates_shared)
        app.router.add_get( "/admin/",                      self._admin_home)
        app.router.add_get( "/admin",                       self._admin_home)
        app.router.add_get( "/admin/clipboard",             self._clip_admin_page)
        app.router.add_get( "/admin/config",                self._clip_admin_get)
        app.router.add_get( "/admin/devices",               self._devices_get)
        app.router.add_post("/admin/devices",               self._devices_set)
        app.router.add_post("/admin/clipboard/clear",       self._clip_clear)
        app.router.add_get( "/admin/tokens",                self._tokens_get)
        app.router.add_post("/admin/tokens",                self._tokens_set)
        app.router.add_post("/admin/config",                self._clip_admin_set)
        # Firewall
        app.router.add_get( "/yay/firewall/rules",          self._fw_rules)
        app.router.add_get( "/yay/firewall/presets",        self._fw_presets)
        app.router.add_post("/yay/firewall/rule",           self._fw_add_rule)
        app.router.add_delete("/yay/firewall/rule",         self._fw_delete_rule)
        app.router.add_put( "/yay/firewall/policy",         self._fw_policy)
        app.router.add_post("/yay/firewall/flush",          self._fw_flush)
        app.router.add_post("/yay/firewall/preset",         self._fw_preset)
        # Bluetooth
        app.router.add_get( "/yay/bluetooth/status",        self._bt_status)
        app.router.add_put( "/yay/bluetooth/power",         self._bt_power)
        app.router.add_put( "/yay/bluetooth/discoverable",  self._bt_discoverable)
        app.router.add_put( "/yay/bluetooth/pairable",      self._bt_pairable)
        app.router.add_get( "/yay/bluetooth/devices",       self._bt_devices)
        app.router.add_post("/yay/bluetooth/scan",          self._bt_scan)
        app.router.add_post("/yay/bluetooth/connect",       self._bt_connect)
        app.router.add_post("/yay/bluetooth/disconnect",    self._bt_disconnect)
        app.router.add_post("/yay/bluetooth/pair",          self._bt_pair)
        app.router.add_put( "/yay/bluetooth/trust",         self._bt_trust)
        app.router.add_delete("/yay/bluetooth/device",      self._bt_remove)
        # Chat history
        app.router.add_get(   "/yay/chat/history",          self._chat_list)
        app.router.add_post(  "/yay/chat/history",          self._chat_save)
        app.router.add_get(   "/yay/chat/history/{id}",     self._chat_load)
        app.router.add_delete("/yay/chat/history/{id}",     self._chat_delete)
        # Apps
        app.router.add_get(   "/yay/apps",                  self._apps_list)
        app.router.add_post(  "/yay/apps/launch",           self._apps_launch)
        app.router.add_delete("/yay/apps/kill",             self._apps_kill)
        # Terminal
        app.router.add_post("/yay/terminal/exec",           self._terminal_exec)
        # WebRTC screen stream
        app.router.add_post(  "/yay/webrtc/offer",          self._webrtc_offer)
        app.router.add_post(  "/yay/webrtc/answer",         self._webrtc_answer)
        app.router.add_delete("/yay/webrtc/peer",           self._webrtc_close)
        app.router.add_get(   "/yay/webrtc/peers",          self._webrtc_peers)
        # Desktop input (xdotool)
        app.router.add_post(  "/yay/desktop/input",         self._desktop_input)
        app.router.add_get(   "/yay/desktop/size",          self._desktop_size)
        app.on_startup.append(self._clip_poll_start)
        app.on_cleanup.append(self._clip_poll_stop)
        return app

    # ── OpenAI-compatible ────────────────────────────────────────────────────

    async def _chat_completions(self, req: web.Request) -> web.StreamResponse:
        token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not self.broker.validate(token, scope="chat"):
            raise web.HTTPForbidden(reason="invalid or missing capability token")
        body = await req.json()
        self.journal.append("ai_query", body.get("model", "unknown"),
                            {"messages": len(body.get("messages", []))})
        provider = self.router.select(body.get("model", ""))
        return await provider.chat(req, body)

    async def _list_models(self, req: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": self.router.list_models()})

    # ── System info ──────────────────────────────────────────────────────────

    async def _system_info(self, req: web.Request) -> web.Response:
        info = await system_info.get()
        return web.json_response({
            **info,
            "os":             platform.system(),
            "os_version":     platform.version(),
            "sda2":           str(self.sda2),
            "data_uuid":      DATA_UUID,
            "journal_events": self.journal.count(),
            "gateway_version": "0.3.0",
            "platform":        "debian-trixie",
            "display":         "x11",
            "init":            "systemd",
            "fs_union":        "aufs",
        })

    # ── Layer management ─────────────────────────────────────────────────────

    async def _get_layers(self, req: web.Request) -> web.Response:
        layers = layer_control.list_layers(self.sda2)
        return web.json_response({"layers": layers, "sda2": str(self.sda2)})

    async def _set_layer(self, req: web.Request) -> web.Response:
        name = req.match_info["name"]
        body = await req.json()
        enable = bool(body.get("active", False))
        try:
            result = layer_control.set_layer(self.sda2, name, enable)
        except ValueError as e:
            raise web.HTTPBadRequest(reason=str(e))
        self.journal.append("layer_mount" if enable else "layer_umount", name)
        LOG.info("layer %s: %s", name, "enabled" if enable else "disabled")
        return web.json_response(result)

    # ── Journal ──────────────────────────────────────────────────────────────

    async def _query_journal(self, req: web.Request) -> web.Response:
        since_raw  = req.rel_url.query.get("since")
        event_type = req.rel_url.query.get("type")
        since_ts   = float(since_raw) if since_raw else None
        events     = self.journal.query(since=since_ts, event_type=event_type)
        return web.json_response({"events": events})

    async def _append_journal(self, req: web.Request) -> web.Response:
        body = await req.json()
        event_type = body.get("type", "")
        subject    = body.get("subject", "")
        detail     = body.get("detail", {})
        if not event_type or not subject:
            raise web.HTTPBadRequest(reason="'type' and 'subject' are required")
        try:
            self.journal.append(event_type, subject, detail)
        except AssertionError as e:
            raise web.HTTPBadRequest(reason=str(e))
        return web.json_response({"ok": True})

    # ── Brightness ────────────────────────────────────────────────────────────

    async def _get_brightness(self, req: web.Request) -> web.Response:
        return web.json_response(brightness.get())

    async def _set_brightness_screen(self, req: web.Request) -> web.Response:
        body = await req.json()
        pct  = int(body.get("pct", 50))
        try:
            result = brightness.set_screen(pct)
        except FileNotFoundError as e:
            raise web.HTTPNotFound(reason=str(e))
        self.journal.append("brightness_change", "screen", {"pct": pct})
        return web.json_response(result)

    async def _set_brightness_kbd(self, req: web.Request) -> web.Response:
        body = await req.json()
        pct  = int(body.get("pct", 50))
        try:
            result = brightness.set_kbd(pct)
        except FileNotFoundError as e:
            raise web.HTTPNotFound(reason=str(e))
        self.journal.append("brightness_change", "kbd", {"pct": pct})
        return web.json_response(result)

    # ── Audio ─────────────────────────────────────────────────────────────────

    async def _get_audio(self, req: web.Request) -> web.Response:
        return web.json_response(await audio.get_status())

    async def _list_sinks(self, req: web.Request) -> web.Response:
        return web.json_response({"sinks": await audio.list_sinks()})

    async def _set_volume(self, req: web.Request) -> web.Response:
        body   = await req.json()
        pct    = int(body.get("pct", 50))
        target = body.get("target", "@DEFAULT_SINK@")
        result = await audio.set_volume(pct, target)
        if result["ok"]:
            self.journal.append("volume_change", target, {"pct": pct})
        return web.json_response(result)

    async def _set_mute(self, req: web.Request) -> web.Response:
        body   = await req.json()
        muted  = bool(body.get("muted", False))
        target = body.get("target", "@DEFAULT_SINK@")
        result = await audio.set_mute(muted, target)
        if result["ok"]:
            self.journal.append("mute_change", target, {"muted": muted})
        return web.json_response(result)

    # ── Health ───────────────────────────────────────────────────────────────

    async def _health(self, req: web.Request) -> web.Response:
        return web.json_response({
            "status":         "ok",
            "hostname":       platform.node(),
            "providers":      self.router.status(),
            "journal_events": self.journal.count(),
            "sda2":           str(self.sda2),
            # Kept for older app builds that read it; "os"/"features" below are
            # what a cross-platform client should look at.
            "platform":       "debian-trixie",
            "os":             platforms.HOST_OS,
            "features":       platforms.features(),
        })

    async def _capabilities(self, req: web.Request) -> web.Response:
        """What this host supports, and whether the pieces are actually
        working (missing packages, ungranted macOS permissions, which audio
        control surface was detected)."""
        info = await platforms.probe()
        try:
            info["audio"] = await audio.describe()
        except Exception as e:
            info["audio"] = {"error": str(e)}
        return web.json_response(info)

    # ── Capability tokens ────────────────────────────────────────────────────

    async def _issue_capability(self, req: web.Request) -> web.Response:
        # Renewal only: same device, same scope and permissions, never more.
        # It used to mint any scope/subject/lifetime for any paired device,
        # which let a device keep spare tokens past being revoked.
        body = await req.json()
        old = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        ttl = max(60, min(int(body.get("ttl", 3600)), 10 * 365 * 24 * 3600))
        token = self.broker.renew(old, ttl)
        if not token:
            return web.json_response({"error": "renewal needs a valid device token"}, status=403)
        return web.json_response({"token": token})

    async def _get_providers(self, req: web.Request) -> web.Response:
        providers = self.config.get("providers", {})
        masked = {}
        for name, cfg in providers.items():
            entry = dict(cfg)
            key = entry.get("api_key", "")
            entry["api_key_set"] = bool(key)
            entry["api_key_preview"] = (key[:8] + "…") if len(key) > 8 else ("" if not key else key)
            entry.pop("api_key", None)
            masked[name] = entry
        return web.json_response({"providers": masked})

    async def _set_providers(self, req: web.Request) -> web.Response:
        body = await req.json()
        if "providers" not in self.config:
            self.config["providers"] = {}
        for provider_name, updates in body.get("providers", {}).items():
            if provider_name not in self.config["providers"]:
                self.config["providers"][provider_name] = {}
            for k, v in updates.items():
                self.config["providers"][provider_name][k] = v
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, indent=2) + "\n")
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass
        self.router = ProviderRouter(self.config.get("providers", {}))
        LOG.info("providers updated and reloaded")
        return web.json_response({"ok": True, "providers": list(self.config["providers"].keys())})

    async def _check_capability(self, req: web.Request) -> web.Response:
        body = await req.json()
        ok = self.broker.validate(body.get("token", ""), scope=body.get("scope", "chat"))
        return web.json_response({"valid": ok})

    # ── Pairing (bootstraps a device's first token) ─────────────────────────
    # /yay/pair/start must stay loopback-only: whoever can see the code it
    # returns can pair any device, anywhere. /yay/pair (spend the code) is
    # deliberately public — the code itself is the credential, one-shot,
    # 10-minute TTL, 4 random bytes hex-encoded.

    def _new_pairing_code(self) -> str:
        self._pairing_code    = secrets.token_hex(4)
        self._pairing_expires = time.time() + 600
        self._pairing_used    = False
        LOG.info("pairing code: %s (valid 10 min)", self._pairing_code)
        return self._pairing_code

    async def _pair_start(self, req: web.Request) -> web.Response:
        if not _is_loopback(req):
            return web.json_response({"error": "loopback only"}, status=403)
        code = self._new_pairing_code()
        return web.json_response({"pairing_code": code, "expires_in": 600})

    async def _pair_device(self, req: web.Request) -> web.Response:
        body = await req.json()
        code = str(body.get("pairing_code", ""))
        now = time.time()
        self._pair_failures = [t for t in self._pair_failures if now - t < 60]
        if len(self._pair_failures) >= 5:          # G9: slow down guessing
            return web.json_response(
                {"error": "too many wrong pairing codes; wait a minute"}, status=429)
        valid = (not self._pairing_used and self._pairing_code is not None
                 and now < self._pairing_expires
                 and hmac.compare_digest(code, self._pairing_code))
        if not valid:
            self._pair_failures.append(now)
            self.journal.append("pair_failed", req.remote or "local")
            return web.json_response(
                {"error": "invalid, expired, or already-used pairing code"}, status=403)
        self._pairing_used = True
        device_name = str(body.get("device_name", "unknown"))[:64]
        token = self.broker.issue(scope="admin", ttl_seconds=10 * 365 * 24 * 3600,
                                  subject=device_name)
        # G3: from the first pairing on, only listed devices get in. Devices
        # that already paired (seen with a name) stay allowed.
        if not self._rules.whitelist:
            keep = [k for k, v in self._rules.seen.items() if v.get("name", "unknown") != "unknown"]
            new = clip_rules.keys_for(req.remote, device_name)[:1]
            self._rules.set_whitelist(True, keep + new)
            self.journal.append("whitelist_change", "on", {"allow": keep + new})
        else:
            self._rules.set_whitelist(True, self._rules.allow + clip_rules.keys_for(req.remote, device_name)[:1])
        self.journal.append("device_paired", device_name, {"remote": req.remote})
        LOG.info("paired device %r", device_name)
        return web.json_response({"token": token, "device_name": device_name})

    # ── Network management (ip / iwlist / wpa_cli — no nmcli) ────────────────

    async def _net_status(self, req: web.Request) -> web.Response:
        # Check default route reachability via `ip route get`
        rc, out, _ = await _run_cmd(["ip", "route", "get", "1.1.1.1"])
        if rc == 0 and out.strip():
            state = "connected"
        else:
            # fallback: any UP non-loopback interface in plain `ip link` = "limited"
            rc2, out2, _ = await _run_cmd(["ip", "link"])
            state = "disconnected"
            if rc2 == 0:
                for line in out2.splitlines():
                    if re.search(r"<[^>]*\bUP\b[^>]*>", line) and "LOOPBACK" not in line:
                        state = "limited"
                        break
        return web.json_response({"state": state, "connectivity": state})

    async def _net_interfaces(self, req: web.Request) -> web.Response:
        rc, out, _ = await _run_cmd(["ip", "addr"])
        if rc != 0:
            return web.json_response({"interfaces": [], "error": "ip command failed"})
        ifaces = _parse_ip_addr(out)
        return web.json_response({"interfaces": ifaces})

    async def _net_tailscale_peers(self, req: web.Request) -> web.Response:
        rc, out, err = await _run_cmd(["tailscale", "status", "--json"])
        if rc != 0:
            return web.json_response({"peers": [], "error": err.strip() or "tailscale status failed"})
        try:
            status = json.loads(out)
        except json.JSONDecodeError:
            return web.json_response({"peers": [], "error": "could not parse tailscale status"})

        def _peer_entry(node: dict, is_self: bool) -> dict | None:
            # Exit-node relays (Mullvad etc.) report no OS and aren't real
            # devices — filtering on OS keeps this to actual tailnet members.
            if not node.get("OS"):
                return None
            ip = next((a for a in node.get("TailscaleIPs", []) if "." in a), None)
            if ip is None:
                return None
            return {
                "hostname": node.get("HostName", ip),
                "ip":       ip,
                "os":       node.get("OS", ""),
                "online":   bool(node.get("Online", is_self)),
                "self":     is_self,
            }

        peers = []
        self_node = status.get("Self")
        if self_node:
            entry = _peer_entry(self_node, is_self=True)
            if entry:
                peers.append(entry)
        for node in (status.get("Peer") or {}).values():
            entry = _peer_entry(node, is_self=False)
            if entry:
                peers.append(entry)
        peers.sort(key=lambda p: (not p["self"], not p["online"], p["hostname"]))
        return web.json_response({"peers": peers})

    async def _qr_gateway_url(self, req: web.Request) -> web.Response:
        """QR code (PNG) encoding this gateway's URL + a fresh pairing
        code as JSON, meant to be viewed in a browser ON THIS PC (e.g.
        http://localhost:8772/yay/network/gateway_qr) so a phone's
        camera can scan the screen, fill in TrXi-Ctrl's Gateway URL
        field, AND pair in one action — solves the one gap "Discover
        Peers" can't: that feature needs an *already-working* gateway
        URL to call into, so it can't help with the very first
        connection to any gateway. Loopback-only, same reasoning as
        /yay/pair/start: this now hands out a pairing secret, not just
        an address."""
        if not _is_loopback(req):
            return web.json_response({"error": "loopback only"}, status=403)
        rc, out, _ = await _run_cmd(["tailscale", "status", "--json"])
        ip = None
        if rc == 0:
            try:
                self_node = json.loads(out).get("Self") or {}
                ip = next((a for a in self_node.get("TailscaleIPs", []) if "." in a), None)
            except json.JSONDecodeError:
                pass
        if not ip:
            return web.json_response(
                {"error": "tailscale IP not available — is tailscale up?"}, status=503)
        url = f"http://{ip}:{self.port}"
        code = self._new_pairing_code()
        payload = json.dumps({"url": url, "pair": code})
        try:
            proc = await asyncio.create_subprocess_exec(
                "qrencode", "-o", "-", "-t", "PNG", "-s", "8", payload,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            png, err = await proc.communicate()
        except FileNotFoundError:
            return web.json_response(
                {"error": "qrencode not installed (apt install qrencode)"}, status=500)
        if proc.returncode != 0 or not png:
            return web.json_response(
                {"error": (err or b"qrencode failed").decode(errors="replace")}, status=500)
        # the code as text too: desktop apps without a camera type it in
        return web.Response(body=png, content_type="image/png",
                            headers={"X-Gateway-URL": url, "X-Pairing-Code": code})

    async def _wifi_scan(self, req: web.Request) -> web.Response:
        iface = req.rel_url.query.get("iface", self.wifi_iface)
        # bring up the interface for scanning if needed
        await _run_cmd(["ip", "link", "set", iface, "up"])
        rc, out, err = await _run_cmd(
            ["iwlist", iface, "scan"], timeout=20)
        if rc != 0:
            return web.json_response({"networks": [], "error": err.strip() or "iwlist failed"})
        networks = _parse_iwlist(out)
        return web.json_response({"networks": networks})

    async def _wifi_connect(self, req: web.Request) -> web.Response:
        body     = await req.json()
        ssid     = body.get("ssid", "").strip()
        password = body.get("password", "").strip()
        iface    = body.get("iface", self.wifi_iface)
        if not ssid:
            raise web.HTTPBadRequest(reason="ssid required")
        ok, msg = await _wpa_connect(iface, ssid, password)
        if ok:
            self.journal.append("wifi_connect", ssid)
        return web.json_response({"ok": ok, "ssid": ssid, "output": msg})

    async def _wifi_disconnect(self, req: web.Request) -> web.Response:
        body  = await req.json()
        iface = body.get("device", self.wifi_iface)
        rc, out, err = await _run_cmd(["ip", "link", "set", iface, "down"])
        ok = rc == 0
        if ok:
            self.journal.append("wifi_disconnect", iface)
        return web.json_response({
            "ok": ok, "device": iface,
            "output": (out + err).strip(),
        })

    # ── Media ─────────────────────────────────────────────────────────────────

    async def _media_sessions(self, req: web.Request) -> web.Response:
        return web.json_response({"sessions": await media.list_sessions()})

    async def _media_status(self, req: web.Request) -> web.Response:
        pid = req.rel_url.query.get("pid")
        return web.json_response(await media.get_status(int(pid) if pid else None))

    async def _media_play(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await media.play(body.get("pid")))

    async def _media_pause(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await media.pause(body.get("pid")))

    async def _media_stop(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await media.stop(body.get("pid")))

    async def _media_volume(self, req: web.Request) -> web.Response:
        body = await req.json()
        vol  = float(body.get("volume", body.get("pct", 100)))
        if vol > 1.0: vol /= 100.0
        return web.json_response(await media.set_volume(vol, body.get("pid")))

    async def _media_seek(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(
            await media.seek(float(body.get("seconds", 0)), body.get("pid")))

    async def _media_open(self, req: web.Request) -> web.Response:
        body   = await req.json()
        path   = body.get("path", "")
        result = await media.open_file(path, body.get("pid"))
        if result.get("ok"):
            self.journal.append("media_open", path)
        return web.json_response(result)

    async def _media_recent(self, req: web.Request) -> web.Response:
        return web.json_response({"files": await media.list_recent()})

    async def _media_clear_recent(self, req: web.Request) -> web.Response:
        return web.json_response(await media.clear_recent())

    async def _media_browse(self, req: web.Request) -> web.Response:
        path = req.rel_url.query.get("path")
        return web.json_response(await media.browse(path))

    # ── PersonalManager ──────────────────────────────────────────────────────
    async def _pm_call(self, coro, journal: str = "") -> web.Response:
        """Bad input (ValueError/KeyError/PermissionError) -> 400 with the
        message, so the phone can show pm_store's validation errors."""
        try:
            result = await coro
        except (ValueError, KeyError, PermissionError, FileNotFoundError) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            return web.json_response({"ok": False, "error": msg}, status=400)
        if journal and result.get("ok"):
            self.journal.append("pm", journal)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _pm_list(self, req: web.Request) -> web.Response:
        return await self._pm_call(personal_manager.list_items(
            req.rel_url.query.get("collection", "")))

    async def _pm_add(self, req: web.Request) -> web.Response:
        c = req.match_info["collection"]
        return await self._pm_call(personal_manager.add_item(c, await req.json()), f"add {c}")

    async def _pm_update(self, req: web.Request) -> web.Response:
        c, i = req.match_info["collection"], req.match_info["id"]
        return await self._pm_call(personal_manager.update_item(c, i, await req.json()),
                                   f"update {c} {i}")

    async def _pm_remove(self, req: web.Request) -> web.Response:
        c, i = req.match_info["collection"], req.match_info["id"]
        return await self._pm_call(personal_manager.remove_item(c, i), f"remove {c} {i}")

    async def _pm_apply(self, req: web.Request) -> web.Response:
        lines = (await req.json()).get("lines", "")
        return await self._pm_call(personal_manager.apply_lines(lines), "apply")

    async def _pm_cron_enabled(self, req: web.Request) -> web.Response:
        body = await req.json()
        return await self._pm_call(personal_manager.set_cron_enabled(
            body.get("match", ""), bool(body.get("enabled", True))),
            f"cron {'resume' if body.get('enabled', True) else 'pause'} {body.get('match', '')}")

    async def _pm_format(self, req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "format": personal_manager.format_help()})

    async def _media_kill(self, req: web.Request) -> web.Response:
        body   = await req.json()
        pid    = body.get("pid")
        if pid is None:
            return web.json_response({"ok": False, "error": "pid required"}, status=400)
        result = await media.kill_session(int(pid))
        if result.get("ok"):
            self.journal.append("media_kill", str(pid))
        return web.json_response(result)

    async def _media_source(self, req: web.Request) -> web.Response:
        body   = await req.json()
        source = body.get("source", "file")
        path   = body.get("path")
        result = await media.launch_source(source, path)
        if result.get("ok"):
            self.journal.append("media_source", source, {"path": path})
        return web.json_response(result)

    # ── Window manager ────────────────────────────────────────────────────────

    async def _wm_list(self, req: web.Request) -> web.Response:
        return web.json_response({"windows": await wm.list_windows()})

    async def _wm_active(self, req: web.Request) -> web.Response:
        return web.json_response(await wm.get_active_window() or {})

    async def _wm_desktops(self, req: web.Request) -> web.Response:
        desktops = await wm.list_desktops()
        current  = await wm.get_current_desktop()
        return web.json_response({"desktops": desktops, "current": current})

    async def _wm_set_desktop(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.set_desktop(int(body["desktop"])))

    async def _wm_focus(self, req: web.Request) -> web.Response:
        body = await req.json()
        wid  = int(body["id"]) if "id" in body else None
        if wid is None and "title" in body:
            wins = await wm.find_windows(title=body["title"])
            wid  = wins[0]["id"] if wins else None
        if wid is None:
            return web.json_response({"ok": False, "error": "window not found"})
        return web.json_response(await wm.focus_window(wid))

    async def _wm_close(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.close_window(int(body["id"])))

    async def _wm_minimize(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.minimize_window(int(body["id"])))

    async def _wm_maximize(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.maximize_window(int(body["id"])))

    async def _wm_restore(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.restore_window(int(body["id"])))

    async def _wm_fullscreen(self, req: web.Request) -> web.Response:
        body   = await req.json()
        enable = bool(body.get("enable", True))
        return web.json_response(await wm.fullscreen_window(int(body["id"]), enable))

    async def _wm_geometry(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.move_resize_window(
            int(body["id"]), int(body["x"]), int(body["y"]),
            int(body["width"]), int(body["height"]),
        ))

    async def _wm_tile(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await wm.tile_windows(int(body.get("cols", 2))))

    # ── Lockscreen ───────────────────────────────────────────────────────────

    async def _lock_status(self, req: web.Request) -> web.Response:
        return web.json_response(await lockscreen.get_status())

    async def _lock_lock(self, req: web.Request) -> web.Response:
        result = await lockscreen.lock()
        if result.get("ok"):
            self.journal.append("lock", "screen_locked")
        return web.json_response(result)

    async def _lock_unlock(self, req: web.Request) -> web.Response:
        result = await lockscreen.unlock()
        if result.get("ok"):
            self.journal.append("unlock", "screen_unlocked", {"source": "remote"})
        return web.json_response(result)

    # ── Clipboard ─────────────────────────────────────────────────────────────
    # PC as the hub: a shared history (capabilities/clipboard.History) of
    # every device's pushes and the PC's own copies, newest first, kept
    # across restarts. Pushing also sets the PC's OS clipboard so it can be
    # pasted into a PC document. Locked items are never trimmed or deleted.

    def _clip_device(self, req: web.Request) -> str:
        """The device label (clip_rules: Tailscale node / MAC / paired name,
        named on /admin/clipboard); "PC" for this machine."""
        return self._clip_ident(req)[0]

    def _clip_ident(self, req: web.Request):
        token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return self._rules.ident(req.remote, self.broker.subject(token) or "unknown")

    def _clip_item(self, req: web.Request):
        """The item named in the URL, if this device may see it."""
        item = self._clip.get(req.match_info["id"])
        if item and self._rules.can_see(self._clip_device(req),
                                        self._rules.shown([item])[0]["device"]):
            return item
        return None

    # ── Phone app updates ─────────────────────────────────────────────────────

    async def _updates_list(self, req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "apps": app_updates.list_apps()})

    async def _updates_download(self, req: web.Request):
        p = app_updates.apk_path(req.match_info["package"], req.match_info["abi"])
        if not p:
            return web.json_response({"ok": False, "error": "no such app"}, status=404)
        return web.FileResponse(p, headers={
            "Content-Type": "application/vnd.android.package-archive"})

    async def _updates_share(self, req: web.Request) -> web.Response:
        """A 24 h download link (no token needed), posted to the shared
        clipboard as this device's item, so others can tap it."""
        body = await req.json() if req.can_read_body else {}
        pkg = req.match_info["package"]
        r = app_updates.share(pkg, str(body.get("abi") or "arm64-v8a"))
        if not r:
            return web.json_response({"ok": False, "error": "no such app"}, status=404)
        token, fname, expires = r
        url = "%s://%s/dl/%s/%s" % (req.scheme, req.host, token, fname)
        app = next((a for a in app_updates.list_apps() if a["package"] == pkg), {})
        text = "%s %s — install link (valid until %s): %s" % (
            app.get("name", pkg), app.get("version", ""),
            time.strftime("%d %b %H:%M", time.localtime(expires)), url)
        device, key = self._clip_ident(req)
        self._clip.add(text, device, key, body.get("to") or None)
        self.journal.append("clipboard_set", device)
        return web.json_response({"ok": True, "url": url, "expires": expires})

    async def _updates_shared(self, req: web.Request):
        p, fname = app_updates.resolve_share(req.match_info["token"])
        if not p:
            return web.Response(status=404, text="This link has expired or is unknown.")
        return web.FileResponse(p, headers={
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": 'attachment; filename="%s"' % fname})

    async def _admin_home(self, req: web.Request) -> web.Response:
        """Local-only home page: pair a phone (QR), status, app updates,
        links to the other settings pages."""
        import html as _h
        apps = "".join(
            "<tr><td>%s</td><td>%s (build %s)</td><td>%s</td></tr>" % (
                _h.escape(a["name"]), _h.escape(a["version"]), a["code"],
                _h.escape(a["published"]))
            for a in app_updates.list_apps()) or \
            "<tr><td colspan=3>None published yet.</td></tr>"
        clips = len(self._clip.items)
        named = len(self._rules.devices)
        import getpass
        account = getpass.getuser()
        note = ("Any device with Terminal allowed can do anything on this PC. Run the "
                "gateway as your normal user unless you need firewall or network control."
                if account in ("root", "Administrator", "SYSTEM") else
                "Root-only features (firewall, network settings) need an administrator.")
        page = ADMIN_HOME % {"apps": apps, "clips": clips, "named": named,
                             "account": _h.escape(account), "account_note": note,
                             "expire": round(self._clip.max_age / 3600) if self._clip.max_age else "never",
                             "port": self.port, "host": _h.escape(platform.node())}
        return web.Response(text=page, content_type="text/html")

    async def _clip_admin_page(self, req: web.Request) -> web.Response:
        return web.Response(text=clip_rules.ADMIN_HTML, content_type="text/html")

    async def _clip_admin_get(self, req: web.Request) -> web.Response:
        return web.json_response(self._rules.data())

    async def _devices_get(self, req: web.Request) -> web.Response:
        r = self._rules
        return web.json_response({
            "whitelist": r.whitelist, "allow": r.allow, "now": time.time(),
            "seen": [dict(v, key=k, label=r.label_of_key(k)) for k, v in r.seen.items()]})

    async def _clip_clear(self, req: web.Request) -> web.Response:
        n = self._clip.clear()
        self.journal.append("clipboard_clear", str(n))
        return web.json_response({"ok": True, "removed": n})

    async def _tokens_get(self, req: web.Request) -> web.Response:
        return web.json_response({"now": time.time(), "idle_limit": self.broker.IDLE_LIMIT,
                                  "tokens": self.broker.tokens()})

    async def _tokens_set(self, req: web.Request) -> web.Response:
        body = await req.json()
        jti = str(body.get("jti", ""))
        ok = self.broker.set_token(jti, revoked=body.get("revoked"), terminal=body.get("terminal"))
        if ok:
            self.journal.append("device_revoked" if body.get("revoked") else "terminal_permission",
                                jti, {k: body[k] for k in ("revoked", "terminal") if k in body})
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def _devices_set(self, req: web.Request) -> web.Response:
        body = await req.json()
        self._rules.set_whitelist(body.get("whitelist"), body.get("allow") or [])
        self.journal.append("whitelist_change", "on" if body.get("whitelist") else "off",
                            {"allow": self._rules.allow})
        return web.json_response({"ok": True})

    async def _clip_admin_set(self, req: web.Request) -> web.Response:
        body = await req.json()
        self._rules.update(body.get("devices"), body.get("groups"), body.get("receive"))
        return web.json_response({"ok": True})

    async def _clip_poll_start(self, app):
        self._clip_task = asyncio.create_task(self._clip_poll())

    async def _clip_poll_stop(self, app):
        self._clip_task.cancel()

    async def _clip_poll(self):
        """The PC's own copies join the history (every 2 s)."""
        while True:
            try:
                pc = await clipboard.get()
                text = pc.get("text") if pc.get("ok") else None
                if text and text.strip() and text != self._pc_seen:
                    first, self._pc_seen = self._pc_seen is None, text
                    # after a (re)start, a text already in the history was
                    # copied before: only note it
                    known = first and any(i["text"] == text
                                          for i in self._clip.items)
                    if not known and text != self._clip.latest_text():
                        self._clip.add(text, "This PC (%s)" % platform.node(),
                                       clip_rules.PC)
            except Exception as e:                     # keep polling
                LOG.debug("clipboard poll: %s", e)
            await asyncio.sleep(2)

    async def _clipboard_get(self, req: web.Request) -> web.Response:
        self._clip.prune()
        me = self._clip_device(req)
        return web.json_response({"ok": True, "me": me,
                                  "entries": self._rules.visible(
                                      me, self._rules.shown(self._clip.items)),
                                  "max": self._clip.max_items,
                                  "targets": self._rules.targets(me),
                                  "muted": self._rules.mute.get(me, [])})

    async def _clipboard_mute(self, req: web.Request) -> web.Response:
        """This device hides (or shows again) one sender's shares."""
        body = await req.json()
        sender = str(body.get("sender", "")).strip()
        if not sender:
            return web.json_response({"ok": False, "error": "no sender"}, status=400)
        if sender.startswith("This PC"):
            sender = clip_rules.PC
        muted = self._rules.set_mute(self._clip_device(req), sender,
                                     bool(body.get("muted", True)))
        return web.json_response({"ok": True, "muted": muted})

    async def _clipboard_set(self, req: web.Request) -> web.Response:
        body = await req.json()
        text = str(body.get("text", ""))
        if not text:
            return web.json_response({"ok": False, "error": "empty"}, status=400)
        device, key = self._clip_ident(req)
        to = [str(x) for x in body.get("to") or [] if str(x).strip()]
        item = self._clip.add(text, device, key, to)
        self._pc_seen = text                  # the poll must not re-add it
        result = await clipboard.set_text(text)
        self.journal.append("clipboard_set", device)
        return web.json_response({"ok": True, "device": device, "id": item["id"],
                                  "pc": result.get("ok", False),
                                  **({"error": result["error"]} if "error" in result else {})})

    async def _clipboard_delete(self, req: web.Request) -> web.Response:
        if self._clip_item(req) is None:
            return web.json_response({"ok": False, "error": "missing"}, status=404)
        r = self._clip.delete(req.match_info["id"])
        if r == "ok":
            self.journal.append("clipboard_delete", self._clip_device(req))
        return web.json_response({"ok": r == "ok", **({"error": r} if r != "ok" else {})},
                                 status={"ok": 200, "locked": 409}.get(r, 404))

    async def _clipboard_lock(self, req: web.Request) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        if self._clip_item(req) is None:
            return web.json_response({"ok": False, "error": "missing"}, status=404)
        item = self._clip.lock(req.match_info["id"], body.get("locked", True))
        return web.json_response({"ok": item is not None, "item": item},
                                 status=200 if item else 404)

    async def _clipboard_to_pc(self, req: web.Request) -> web.Response:
        item = self._clip_item(req)
        if item is None:
            return web.json_response({"ok": False, "error": "missing"}, status=404)
        self._pc_seen = item["text"]
        result = await clipboard.set_text(item["text"])
        return web.json_response({"ok": result.get("ok", False),
                                  **({"error": result["error"]} if "error" in result else {})})

    async def _clipboard_save(self, req: web.Request) -> web.Response:
        if self._clip_item(req) is None:
            return web.json_response({"ok": False, "error": "missing"}, status=404)
        path = self._clip.save_file(req.match_info["id"])
        if path:
            self.journal.append("clipboard_save", path)
        return web.json_response({"ok": bool(path), "path": path},
                                 status=200 if path else 404)

    # ── Firewall ──────────────────────────────────────────────────────────────

    async def _fw_rules(self, req: web.Request) -> web.Response:
        return web.json_response(await firewall.list_rules())

    async def _fw_presets(self, req: web.Request) -> web.Response:
        return web.json_response({
            "presets": [
                {"name": k, "description": v["description"]}
                for k, v in firewall.PRESETS.items()
            ]
        })

    async def _fw_add_rule(self, req: web.Request) -> web.Response:
        body = await req.json()
        result = await firewall.add_rule(
            chain     = body["chain"],
            target    = body["target"],
            proto     = body.get("proto"),
            src       = body.get("src"),
            dst       = body.get("dst"),
            dport     = body.get("dport"),
            sport     = body.get("sport"),
            iface_in  = body.get("iface_in"),
            iface_out = body.get("iface_out"),
            insert    = body.get("insert", False),
            comment   = body.get("comment"),
        )
        if result.get("ok"):
            self.journal.append("firewall_rule_add", body.get("chain", ""), body)
        return web.json_response(result)

    async def _fw_delete_rule(self, req: web.Request) -> web.Response:
        body = await req.json()
        result = await firewall.delete_rule(body["chain"], int(body["num"]))
        if result.get("ok"):
            self.journal.append("firewall_rule_del", body.get("chain", ""),
                                {"num": body["num"]})
        return web.json_response(result)

    async def _fw_policy(self, req: web.Request) -> web.Response:
        body = await req.json()
        result = await firewall.set_policy(body["chain"], body["policy"])
        if result.get("ok"):
            self.journal.append("firewall_policy", body["chain"],
                                {"policy": body["policy"]})
        return web.json_response(result)

    async def _fw_flush(self, req: web.Request) -> web.Response:
        body  = await req.json()
        chain = body.get("chain")
        result = await firewall.flush_chain(chain)
        if result.get("ok"):
            self.journal.append("firewall_flush", chain or "all")
        return web.json_response(result)

    async def _fw_preset(self, req: web.Request) -> web.Response:
        body   = await req.json()
        name   = body.get("name", "")
        result = await firewall.apply_preset(name)
        if result.get("ok"):
            self.journal.append("firewall_preset", name)
        return web.json_response(result)

    # ── Bluetooth ─────────────────────────────────────────────────────────────

    async def _bt_status(self, req: web.Request) -> web.Response:
        return web.json_response(await bluetooth.get_status())

    async def _bt_power(self, req: web.Request) -> web.Response:
        body = await req.json()
        result = await bluetooth.set_power(bool(body.get("on", True)))
        if result["ok"]:
            self.journal.append("bluetooth_power", "on" if body.get("on") else "off")
        return web.json_response(result)

    async def _bt_discoverable(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await bluetooth.set_discoverable(bool(body.get("on", True))))

    async def _bt_pairable(self, req: web.Request) -> web.Response:
        body = await req.json()
        return web.json_response(await bluetooth.set_pairable(bool(body.get("on", True))))

    async def _bt_devices(self, req: web.Request) -> web.Response:
        only_connected = req.rel_url.query.get("connected") == "true"
        devices = await bluetooth.list_devices(only_connected=only_connected)
        return web.json_response({"devices": devices})

    async def _bt_scan(self, req: web.Request) -> web.Response:
        body     = await req.json()
        duration = int(body.get("duration", 10))
        found    = await bluetooth.scan(duration=min(duration, 30))
        return web.json_response({"found": found, "count": len(found)})

    async def _bt_connect(self, req: web.Request) -> web.Response:
        body   = await req.json()
        addr   = body.get("address", "")
        result = await bluetooth.connect(addr)
        if result["ok"]:
            self.journal.append("bluetooth_connect", addr)
        return web.json_response(result)

    async def _bt_disconnect(self, req: web.Request) -> web.Response:
        body   = await req.json()
        addr   = body.get("address", "")
        result = await bluetooth.disconnect(addr)
        if result["ok"]:
            self.journal.append("bluetooth_disconnect", addr)
        return web.json_response(result)

    async def _bt_pair(self, req: web.Request) -> web.Response:
        body   = await req.json()
        addr   = body.get("address", "")
        result = await bluetooth.pair(addr)
        if result["ok"]:
            self.journal.append("bluetooth_pair", addr)
        return web.json_response(result)

    async def _bt_trust(self, req: web.Request) -> web.Response:
        body    = await req.json()
        addr    = body.get("address", "")
        trusted = bool(body.get("trusted", True))
        return web.json_response(await bluetooth.trust(addr, trusted))

    async def _bt_remove(self, req: web.Request) -> web.Response:
        body   = await req.json()
        addr   = body.get("address", "")
        result = await bluetooth.remove_device(addr)
        if result["ok"]:
            self.journal.append("bluetooth_remove", addr)
        return web.json_response(result)

    # ── Chat history ─────────────────────────────────────────────────────────

    async def _chat_list(self, req: web.Request) -> web.Response:
        return web.json_response({"conversations": chat_history.list_conversations()})

    async def _chat_save(self, req: web.Request) -> web.Response:
        body     = await req.json()
        title    = body.get("title", "Untitled")
        model    = body.get("model", "")
        messages = body.get("messages", [])
        return web.json_response(chat_history.save_conversation(title, model, messages))

    async def _chat_load(self, req: web.Request) -> web.Response:
        conv = chat_history.load_conversation(req.match_info["id"])
        if conv is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(conv)

    async def _chat_delete(self, req: web.Request) -> web.Response:
        return web.json_response(chat_history.delete_conversation(req.match_info["id"]))

    # ── Apps ─────────────────────────────────────────────────────────────────

    async def _apps_list(self, req: web.Request) -> web.Response:
        return web.json_response({"apps": await apps.list_apps()})

    async def _apps_launch(self, req: web.Request) -> web.Response:
        body   = await req.json()
        app_id = body.get("id", "")
        result = await apps.launch_app(app_id)
        if result.get("ok"):
            self.journal.append("app_launch", app_id)
        return web.json_response(result)

    async def _apps_kill(self, req: web.Request) -> web.Response:
        body   = await req.json()
        app_id = body.get("id", "")
        result = await apps.kill_app(app_id)
        if result.get("ok"):
            self.journal.append("app_kill", app_id)
        return web.json_response(result)

    # ── Terminal ──────────────────────────────────────────────────────────────

    # ── WebRTC screen stream ──────────────────────────────────────────────────

    async def _webrtc_offer(self, req: web.Request) -> web.Response:
        body   = await req.json()
        width  = int(body.get("width",  1280))
        height = int(body.get("height", 800))
        fps    = int(body.get("fps",    20))
        video  = bool(body.get("video", True))
        audio  = bool(body.get("audio", True))
        result = await webrtc_screen.create_offer(
            width=width, height=height, fps=fps, video=video, audio=audio)
        if result.get("ok"):
            kind = "audio-only" if not video else f"{width}x{height}@{fps}"
            self.journal.append("webrtc_offer", kind)
        return web.json_response(result)

    async def _webrtc_answer(self, req: web.Request) -> web.Response:
        body    = await req.json()
        peer_id = body.get("peer_id", "")
        sdp     = body.get("sdp", "")
        sdp_type = body.get("type", "answer")
        if not peer_id or not sdp:
            return web.json_response({"ok": False, "error": "peer_id and sdp required"}, status=400)
        return web.json_response(
            await webrtc_screen.set_answer(peer_id, sdp, sdp_type))

    async def _webrtc_close(self, req: web.Request) -> web.Response:
        body    = await req.json()
        peer_id = body.get("peer_id", "")
        result  = await webrtc_screen.close_peer(peer_id)
        if result.get("ok"):
            self.journal.append("webrtc_close", peer_id)
        return web.json_response(result)

    async def _webrtc_peers(self, req: web.Request) -> web.Response:
        return web.json_response({"peers": await webrtc_screen.list_peers()})

    # ── Desktop input ─────────────────────────────────────────────────────────

    async def _desktop_input(self, req: web.Request) -> web.Response:
        body   = await req.json()
        action = body.get("action", "")
        x      = int(body.get("x", 0))
        y      = int(body.get("y", 0))
        device = req.get("device")
        if action == "move":
            return web.json_response(await desktop_input.mouse_move(x, y, device=device))
        if action == "click":
            return web.json_response(await desktop_input.mouse_click(
                x, y, int(body.get("button", 1)), device=device))
        if action == "down":
            return web.json_response(await desktop_input.mouse_down(
                x, y, int(body.get("button", 1)), device=device))
        if action == "up":
            return web.json_response(await desktop_input.mouse_up(
                x, y, int(body.get("button", 1))))
        if action == "scroll":
            return web.json_response(await desktop_input.scroll(
                x, y, body.get("direction", "down"), int(body.get("amount", 3))))
        if action == "type":
            return web.json_response(await desktop_input.key_type(
                body.get("text", "")))
        if action == "key":
            return web.json_response(await desktop_input.key_press(
                body.get("key", "")))
        return web.json_response({"ok": False, "error": f"unknown action: {action}"}, status=400)

    async def _desktop_size(self, req: web.Request) -> web.Response:
        return web.json_response(await desktop_input.get_display_size())

    async def _terminal_exec(self, req: web.Request) -> web.Response:
        body    = await req.json()
        cmd     = body.get("cmd", "").strip()
        cwd     = body.get("cwd")
        timeout = int(body.get("timeout", 30))
        if not cmd:
            return web.json_response({"ok": False, "error": "cmd required"}, status=400)
        tok = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if req.remote and not self.broker.can_terminal(tok):
            # 200 so every app version shows the reason in the terminal
            return web.json_response({
                "ok": False, "returncode": -1, "stdout": "",
                "stderr": "The terminal is off for this device. Turn it on for this device "
                          "on the PC: http://localhost:8772/admin (Paired devices)."})
        why = terminal.dangers(cmd)
        if why and not body.get("confirm"):
            # 200 so older app builds show the stderr text instead of failing
            return web.json_response({
                "ok": False, "confirm_required": True, "reasons": why, "returncode": -1,
                "stdout": "", "stderr": "Not run: this command " + "; ".join(why) +
                ". Confirm it in an up-to-date TrXi-Ctrl to run it anyway."})
        result = await terminal.exec_cmd(cmd, cwd=cwd, timeout=timeout)
        self.journal.append("terminal_exec", cmd[:4000],
                            {"device": req.get("device"), "remote": req.remote,
                             "returncode": result.get("returncode"),
                             **({"confirmed_dangerous": why} if why else {})})
        return web.json_response(result)

    # ── Run ──────────────────────────────────────────────────────────────────

    async def run(self, socket_path: str, port: int):
        self.port = port  # read by _qr_gateway_url to build the QR's URL
        runner = web.AppRunner(self.app)
        await runner.setup()

        sites = []

        try:
            Path(socket_path).unlink(missing_ok=True)
            unix = web.UnixSite(runner, socket_path)
            await unix.start()
            os.chmod(socket_path, 0o660)
            LOG.info("listening on unix:%s", socket_path)
            sites.append(unix)
        except Exception as e:
            LOG.warning("unix socket failed: %s", e)

        if port:
            tcp = web.TCPSite(runner, "0.0.0.0", port)
            await tcp.start()
            LOG.info("listening on http://0.0.0.0:%d", port)
            sites.append(tcp)

        if not sites:
            raise RuntimeError("no listener started")

        await asyncio.Event().wait()


# ── Network helpers ───────────────────────────────────────────────────────────

def _classify_iface(name: str) -> str:
    if name.startswith(("wlan", "wl")):        return "wifi"
    if name.startswith(("eth", "en")):         return "ethernet"
    if name.startswith(("tailscale", "ts")):   return "tailscale"
    if name.startswith("bnep"):                return "bluetooth-pan"
    if name.startswith("usb"):                 return "usb"
    if name.startswith("docker"):              return "docker"
    if name.startswith("br"):                  return "bridge"
    return "other"


def _parse_ip_addr(out: str) -> list[dict]:
    """Parse BusyBox `ip addr` plain-text output into interface dicts."""
    ifaces, current = [], None
    for line in out.splitlines():
        # Header: "3: wlan2: <FLAGS> mtu 1500 ... state DOWN ..."
        m = re.match(r"^\d+:\s+(\S+?):\s+<([^>]*)>.*mtu\s+(\d+).*state\s+(\w+)", line)
        if m:
            if current and "LOOPBACK" not in current["_flags"]:
                ifaces.append(_iface_dict(current))
            flags_str = m.group(2)
            current = {
                "_flags": flags_str,
                "device":    m.group(1).rstrip("@"),
                "mtu":       int(m.group(3)),
                "state":     m.group(4).lower(),
                "mac":       "",
                "addresses": [],
            }
            continue
        if current is None:
            continue
        # MAC: "    link/ether e8:f4:08:e5:dc:c9 ..."
        m = re.match(r"\s+link/\w+\s+([0-9a-f:]{17})", line)
        if m:
            current["mac"] = m.group(1)
            continue
        # IPv4: "    inet 10.157.39.229/24 ..."
        m = re.match(r"\s+inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if m:
            current["addresses"].append(m.group(1))
            continue
        # IPv6: "    inet6 fe80::1/64 scope link ..."  — skip link-local
        m = re.match(r"\s+inet6\s+(\S+)\s+scope\s+(\w+)", line)
        if m and m.group(2) != "link":
            current["addresses"].append(m.group(1))
    if current and "LOOPBACK" not in current["_flags"]:
        ifaces.append(_iface_dict(current))
    return ifaces


def _iface_dict(raw: dict) -> dict:
    name = raw["device"]
    return {
        "device":    name,
        "state":     raw["state"],
        "mac":       raw["mac"],
        "addresses": raw["addresses"],
        "type":      _classify_iface(name),
        "mtu":       raw["mtu"],
    }


def _parse_iwlist(out: str) -> list[dict]:
    """Parse `iwlist <iface> scan` output into a list of network dicts."""
    import re
    networks, current = [], {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Cell "):
            if current.get("ssid"):
                networks.append(current)
            current = {}
        m = re.search(r'ESSID:"([^"]*)"', line)
        if m:
            current["ssid"] = m.group(1)
        m = re.search(r"Signal level[=:](-?\d+)", line)
        if m:
            dbm = int(m.group(1))
            # convert dBm to 0-100 quality (rough: -30=100, -90=0)
            current["signal"] = max(0, min(100, 2 * (dbm + 100)))
        m = re.search(r"Encryption key:(on|off)", line)
        if m:
            current["security"] = "WPA2" if m.group(1) == "on" else "open"
        m = re.search(r"Address: ([0-9A-Fa-f:]{17})", line)
        if m:
            current["bssid"] = m.group(1)
        current.setdefault("active", False)
    if current.get("ssid"):
        networks.append(current)
    # deduplicate by ssid, keep strongest signal
    seen: dict[str, dict] = {}
    for n in networks:
        ssid = n["ssid"]
        if ssid not in seen or n.get("signal", 0) > seen[ssid].get("signal", 0):
            seen[ssid] = n
    return sorted(seen.values(), key=lambda n: n.get("signal", 0), reverse=True)


async def _wpa_connect(iface: str, ssid: str, password: str) -> tuple[bool, str]:
    """Connect to WiFi using wpa_cli if a wpa_supplicant is running,
    otherwise write a minimal wpa_supplicant.conf and launch it."""
    # Try wpa_cli first (if wpa_supplicant is already running)
    rc, out, _ = await _run_cmd(["wpa_cli", "-i", iface, "status"], timeout=3)
    if rc == 0:
        return await _wpa_cli_connect(iface, ssid, password)
    # Otherwise start wpa_supplicant with a temp config
    return await _wpa_start_and_connect(iface, ssid, password)


async def _wpa_cli_connect(iface: str, ssid: str, password: str) -> tuple[bool, str]:
    async def wpa(*args):
        rc, out, err = await _run_cmd(["wpa_cli", "-i", iface, *args], timeout=10)
        return rc == 0, out.strip()

    ok, _ = await wpa("disconnect")
    ok, nid_out = await wpa("add_network")
    nid = nid_out.strip().split()[-1]
    await wpa("set_network", nid, "ssid", f'"{ssid}"')
    if password:
        await wpa("set_network", nid, "psk",  f'"{password}"')
    else:
        await wpa("set_network", nid, "key_mgmt", "NONE")
    await wpa("enable_network", nid)
    ok, msg = await wpa("reconnect")
    return ok, msg

async def _wpa_start_and_connect(iface: str, ssid: str, password: str) -> tuple[bool, str]:
    conf_path = Path(f"/tmp/wpa_{iface}.conf")
    if password:
        conf = (
            f'network={{\n'
            f'    ssid="{ssid}"\n'
            f'    psk="{password}"\n'
            f'}}\n'
        )
    else:
        conf = (
            f'network={{\n'
            f'    ssid="{ssid}"\n'
            f'    key_mgmt=NONE\n'
            f'}}\n'
        )
    conf_path.write_text(conf)
    await _run_cmd(["ip", "link", "set", iface, "up"])
    rc, out, err = await _run_cmd([
        "wpa_supplicant", "-B", "-i", iface,
        "-c", str(conf_path),
        "-P", f"/tmp/wpa_{iface}.pid",
    ], timeout=15)
    if rc != 0:
        return False, err.strip()
    # request DHCP — try whichever client this box has (Trixie: dhcpcd)
    for dhcp in (["dhcpcd", "-4", "-n", iface],
                 ["udhcpc", "-i", iface, "-q", "-n"],
                 ["dhclient", "-4", iface]):
        rc2, out2, err2 = await _run_cmd(dhcp, timeout=20)
        if rc2 == 0:
            break
    return rc2 == 0, (out2 + err2).strip()

async def _run_cmd(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", f"{cmd[0]}: not found"

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="AI gateway daemon — Debian Trixie edition")
    p.add_argument("--socket",  default=DEFAULT_SOCKET)
    p.add_argument("--port",    type=int, default=DEFAULT_PORT,
                   help="TCP port for LAN/phone access (0 = disable)")
    p.add_argument("--config",  type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--pidfile", type=Path, default=None)
    p.add_argument("--wifi-iface", default=WIFI_IFACE,
                   help=f"WiFi interface name (auto-detected: {WIFI_IFACE})")
    p.add_argument("--debug",   action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Pro edition gate (only when license_client.py is present, see the
    # import above). Same tier number ("2", PC software) and backend as
    # Kameleon Presenter's license check. Hard gate, no offline trial:
    # request_or_refresh_token keeps a still-valid cached token across a
    # backend hiccup on its own.
    if license_client is None:
        LOG.info("edition: free")
    elif os.environ.get("TRIXIE_GATEWAY_PRO_DEV_SKIP_LICENSE") == "1":
        # Local testing only, while license-backend isn't deployed yet --
        # never set this for a real install. Loud on purpose.
        LOG.warning(
            "edition: Pro -- DEV MODE: license check SKIPPED "
            "(TRIXIE_GATEWAY_PRO_DEV_SKIP_LICENSE=1). Unset this for anything real.")
    elif not license_client.request_or_refresh_token(2):
        LOG.error(
            "trixie-gateway Pro requires an active Tier 2 (PC software) "
            "license for this device (%s). Manage/purchase at %s -- or remove "
            "license_client.py to run the free edition.",
            license_client.state.last_denial_reason or "backend unreachable",
            license_client.LICENSE_BACKEND_URL,
        )
        raise SystemExit(1)
    else:
        LOG.info("edition: Pro (licensed)")

    gw = TrixieGateway(args.config, wifi_iface=args.wifi_iface)

    if args.pidfile:
        args.pidfile.parent.mkdir(parents=True, exist_ok=True)
        args.pidfile.write_text(str(os.getpid()) + "\n")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, loop.stop)
        except NotImplementedError:
            # Windows' asyncio loop has no add_signal_handler; fall back to a
            # plain signal handler that stops the loop from the main thread.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(loop.stop))

    try:
        loop.run_until_complete(gw.run(args.socket, args.port))
    except RuntimeError as e:
        # loop.stop() from the SIGTERM/SIGINT handler unblocks run_until_complete
        # with "Event loop stopped before Future completed" — that is a clean exit.
        if "Event loop stopped" not in str(e):
            raise
        LOG.info("shutting down on signal")
    finally:
        try:
            Path(args.socket).unlink(missing_ok=True)
        except OSError:
            pass
        loop.close()
        if args.pidfile and args.pidfile.exists():
            args.pidfile.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
