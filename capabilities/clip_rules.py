# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# All rights reserved.
#
# This software is proprietary. No licence is granted to use, copy, modify,
# distribute, sublicense or sell it, in whole or in part, without the prior
# written permission of the copyright holder. Licensing: cou645@gmail.com
#
# Copyright is held by the author personally. CHAMELEON-AI-AGENT LTD has not
# paid for this software or for the hours spent writing it, and holds no
# rights in it.
# Donations via PayPal: cou645@gmail.com
"""Who receives whose clipboard shares (set up on the PC: /admin/clipboard).

Identity: phones often pair with the same name ("TrXi-Ctrl"), so a request
is matched by where it comes from: its Tailscale node (stable on any
network) or, on the LAN, its MAC; the paired name is the fallback. The
admin names devices ("Pixel") and gives each one or more of those keys.

Rules: receive[label] = ["*"] (everyone, the default) or a list of device
labels and "@group"s. The PC sees everything; a device always sees its
own shares.

    {"devices": {"Pixel": ["ts:pixel-8", "mac:6e:6c:..."]},
     "groups":  {"family": ["Pixel", "Doogee"]},
     "receive": {"Pixel": ["Doogee"]},
     "seen":    {"ts:pixel-8": {"ip": "100.98.53.60", "name": "TrXi-Ctrl",
                                "last": 1790971153.0}}}
"""
import ipaddress
import json
import os
import subprocess
import time

RULES = os.path.expanduser("~/.config/trixie-gateway/clipboard_rules.json")
PC = "PC"
_TS_NET = ipaddress.ip_network("100.64.0.0/10")


def is_local(ip):
    return not ip or ip in ("127.0.0.1", "::1")


def _tailscale_names(cache={"at": 0.0, "map": {}}):
    """{tailscale ip: host name}, refreshed at most once a minute."""
    if time.time() - cache["at"] > 60:
        cache["at"] = time.time()
        try:
            out = subprocess.run(["tailscale", "status", "--json"],
                                 capture_output=True, text=True, timeout=5)
            d = json.loads(out.stdout or "{}")
            peers = list((d.get("Peer") or {}).values()) + [d.get("Self") or {}]
            cache["map"] = {ip: p.get("HostName", "")
                            for p in peers for ip in p.get("TailscaleIPs") or []}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return cache["map"]


def _mac(ip):
    try:
        out = subprocess.run(["ip", "neigh", "show", ip], capture_output=True,
                             text=True, timeout=3).stdout.split()
        return out[out.index("lladdr") + 1] if "lladdr" in out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def keys_for(ip, name):
    """Identity keys of a request, strongest first."""
    keys = []
    try:
        on_ts = ip and ipaddress.ip_address(ip) in _TS_NET
    except ValueError:
        on_ts = False
    if on_ts:
        host = _tailscale_names().get(ip)
        keys.append("ts:" + (host or ip))
    elif ip and not is_local(ip):
        mac = _mac(ip)
        keys.append("mac:" + mac if mac else "ip:" + ip)
    if name and name != "unknown":
        keys.append("name:" + name)
    return keys


class Rules:
    def __init__(self, path=RULES):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.devices = d.get("devices", {})
        self.groups = d.get("groups", {})
        self.receive = d.get("receive", {})
        self.seen = d.get("seen", {})
        self.mute = d.get("mute", {})        # receiver -> muted senders
        self._dirty_at = 0.0

    def data(self):
        return {"devices": self.devices, "groups": self.groups,
                "receive": self.receive, "seen": self.seen, "mute": self.mute}

    def save(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def update(self, devices=None, groups=None, receive=None):
        """From the settings page; unknown labels in rules are dropped."""
        if devices is not None:
            self.devices = {str(k): [str(x) for x in v]
                            for k, v in devices.items() if str(k).strip()}
        if groups is not None:
            self.groups = {str(k): [str(x) for x in v]
                           for k, v in groups.items() if str(k).strip()}
        if receive is not None:
            self.receive = {str(k): [str(x) for x in v]
                            for k, v in receive.items()}
        self.save()

    def label(self, ip, name):
        return self.ident(ip, name)[0]

    def label_of_key(self, key):
        """Current name of a connection key (names can be given later)."""
        if key == PC:
            return PC
        for lab, ks in self.devices.items():
            if key in ks:
                return lab
        return key.split(":", 1)[1] if ":" in key else key

    def shown(self, items):
        """Items with each sender under its current device name."""
        return [dict(i, device=self.label_of_key(i["key"]))
                if i.get("key") not in (None, PC) else i for i in items]

    def ident(self, ip, name):
        """(device label, connection key) for a request; remembers it."""
        if is_local(ip):
            return PC, PC
        keys = keys_for(ip, name)
        now = time.time()
        for k in keys[:1]:                 # the strongest key is the client
            entry = self.seen.setdefault(k, {})
            entry.update(ip=ip, name=name, last=now)
        if now - self._dirty_at > 30:      # don't write the file every poll
            self._dirty_at = now
            self.save()
        for k in keys:
            for lab, ks in self.devices.items():
                if k in ks:
                    return lab, keys[0]
        return (keys[0].split(":", 1)[1], keys[0]) if keys else \
            ("unknown", "name:unknown")

    def senders_for(self, receiver):
        """None = everyone, else the set of sender labels allowed."""
        rule = self.receive.get(receiver)
        if not rule or "*" in rule:
            return None
        out = set()
        for x in rule:
            if x.startswith("@"):
                out |= set(self.groups.get(x[1:], []))
            else:
                out.add(x)
        return out

    def expand(self, names):
        """Labels named directly or through "@group"."""
        out = set()
        for x in names or ():
            out |= set(self.groups.get(x[1:], [])) if x.startswith("@") \
                else {x}
        return out

    def can_see(self, receiver, sender, to=None, muted=True):
        """The PC rules are the ceiling; then the sender's "to" list and
        the receiver's mutes narrow it. The PC (hub) and the sender see
        everything of their own."""
        if sender.startswith("This PC"):   # the PC's own copies
            sender = PC
        if receiver == PC or receiver == sender:
            return True
        allowed = self.senders_for(receiver)
        if allowed is not None and sender not in allowed:
            return False
        if to and receiver not in self.expand(to):
            return False
        return not (muted and sender in self.mute.get(receiver, []))

    def visible(self, receiver, items):
        return [i for i in items
                if self.can_see(receiver, i["device"], i.get("to"))]

    def targets(self, sender):
        """Who a sender can address: named devices and groups."""
        return sorted(lab for lab in self.devices if lab != sender) + \
            sorted("@" + g for g in self.groups)

    def set_mute(self, receiver, sender, on=True):
        lst = [x for x in self.mute.get(receiver, []) if x != sender]
        if on:
            lst.append(sender)
        self.mute[receiver] = lst
        self.save()
        return lst


def _selftest():
    import tempfile
    d = tempfile.mkdtemp()
    r = Rules(os.path.join(d, "r.json"))
    r.update(devices={"Pixel": ["ts:pixel-8"], "Doogee": ["ts:s100pro"],
                      "Oukitel": ["mac:aa:bb:cc:dd:ee:ff"]},
             groups={"family": ["Doogee", "Oukitel"]},
             receive={"Pixel": ["Doogee"], "Oukitel": ["@family", PC]})
    items = [{"device": "This PC (host)", "text": "pc"}, {"device": "Doogee", "text": "d"},
             {"device": "Oukitel", "text": "o"}, {"device": "Pixel", "text": "p"}]
    see = lambda who: [i["text"] for i in r.visible(who, items)]
    assert see(PC) == ["pc", "d", "o", "p"]          # the hub sees all
    assert see("Pixel") == ["d", "p"]                # Doogee + its own
    assert see("Oukitel") == ["pc", "d", "o"]        # group + PC
    assert see("Doogee") == ["pc", "d", "o", "p"]    # no rule: everyone
    # send-to narrows, mute hides, the PC rules stay the ceiling
    msg = {"device": "Doogee", "text": "to Pixel", "to": ["Pixel"]}
    assert [i["text"] for i in r.visible("Pixel", [msg])] == ["to Pixel"]
    assert r.visible("Oukitel", [msg]) == []          # not addressed
    assert r.visible("This PC", [msg]) == [] and r.visible(PC, [msg])
    grp = dict(msg, to=["@family"])
    assert r.visible("Oukitel", [grp]) and not r.visible("Pixel", [grp])
    r.set_mute("Pixel", "Doogee")
    assert r.visible("Pixel", [msg]) == [] and Rules(r.path).mute["Pixel"]
    r.set_mute("Pixel", "Doogee", False)
    assert r.visible("Pixel", [msg])
    oukitel_to_pixel = {"device": "Oukitel", "text": "x", "to": ["Pixel"]}
    assert r.visible("Pixel", [oukitel_to_pixel]) == []   # rule: Doogee only
    assert r.targets("Pixel") == ["Doogee", "Oukitel", "@family"]
    assert r.label("127.0.0.1", "x") == PC
    old = [{"device": "s100pro", "key": "ts:s100pro", "text": "early"}]
    assert r.shown(old)[0]["device"] == "Doogee"   # named after it shared
    assert keys_for("10.0.0.9", "TrXi-Ctrl")[-1] == "name:TrXi-Ctrl"
    assert Rules(r.path).receive["Pixel"] == ["Doogee"]   # saved
    assert oct(os.stat(r.path).st_mode)[-3:] == "600"
    print("clip_rules selftest OK")


if __name__ == "__main__":
    _selftest()


ADMIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipboard sharing — trixie-gateway</title>
<style>
 body{font:15px/1.4 system-ui,sans-serif;margin:0 auto;max-width:980px;padding:16px;background:#111;color:#eee}
 h1{font-size:1.3em} h2{font-size:1.1em;margin-top:1.6em}
 table{border-collapse:collapse;width:100%} th,td{border:1px solid #444;padding:5px 7px;text-align:left}
 th{background:#222} input[type=text]{background:#000;color:#fff;border:1px solid #888;padding:4px;width:12em}
 button{background:#2563eb;color:#fff;border:0;padding:7px 14px;border-radius:4px;font-size:1em;cursor:pointer}
 button.small{background:#333;padding:3px 8px}
 :focus-visible{outline:3px solid #facc15;outline-offset:2px}
 .muted{color:#aaa} .ok{color:#4ade80} .err{color:#f87171} td.c{text-align:center}
</style></head><body>
<h1>Clipboard sharing</h1>
<p class="muted">The PC receives every share. A device always sees its own. Rules decide what each
other device receives. Changes apply on the phones' next refresh (3 s).</p>

<h2>1. Devices</h2>
<p class="muted">Each line is a connection the gateway has seen: a Tailscale node, a LAN MAC, or a paired
name. Give it a device name; several lines can share one name (the same phone on Tailscale and home Wi-Fi).</p>
<table id="seen"><thead><tr><th>Connection</th><th>Last address</th><th>Paired as</th><th>Last seen</th><th>Device name</th></tr></thead><tbody></tbody></table>

<h2>2. Groups</h2>
<p class="muted">Named sets of devices, usable in the rules below.</p>
<div id="groups"></div>
<p><label>New group: <input type="text" id="newgroup"></label> <button class="small" id="addgroup">Add</button></p>

<h2>3. Who receives from whom</h2>
<p class="muted">A device appears here once it has a name (section 1). Untick "Everyone" to choose
senders; a device with no rule receives from everyone.</p>
<table id="rules"></table>

<p><button id="save">Save</button> <span id="msg" role="status" aria-live="polite"></span></p>
<script>
let cfg = {};
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function labels() {                       // device names in use
  const set = new Set(Object.keys(cfg.devices));
  document.querySelectorAll('#seen input').forEach(i => { if (i.value.trim()) set.add(i.value.trim()); });
  return [...set].filter(l => l !== 'PC').sort();
}
function labelOf(key) {
  for (const [l, ks] of Object.entries(cfg.devices)) if (ks.includes(key)) return l;
  return '';
}
function drawSeen() {
  const rows = Object.entries(cfg.seen).sort((a, b) => b[1].last - a[1].last);
  $('#seen tbody').innerHTML = rows.map(([k, s]) =>
    `<tr><td>${esc(k)}</td><td>${esc(s.ip || '')}</td><td>${esc(s.name || '')}</td>
     <td>${new Date(s.last * 1000).toLocaleString()}</td>
     <td><input type="text" data-key="${esc(k)}" value="${esc(labelOf(k))}" aria-label="Device name for ${esc(k)}"></td></tr>`
  ).join('') || '<tr><td colspan=5 class="muted">No phone has connected yet.</td></tr>';
  document.querySelectorAll('#seen input').forEach(i => i.addEventListener('change', () => { collect(); drawGroups(); drawRules(); }));
}
function drawGroups() {
  const ls = labels();
  $('#groups').innerHTML = Object.entries(cfg.groups).map(([g, m]) =>
    `<fieldset><legend>${esc(g)} <button class="small" data-del="${esc(g)}">Remove group</button></legend>` +
    ls.map(l => `<label><input type="checkbox" data-group="${esc(g)}" value="${esc(l)}" ${m.includes(l) ? 'checked' : ''}> ${esc(l)}</label> `).join('') +
    `</fieldset>`).join('') || '<p class="muted">No groups.</p>';
  document.querySelectorAll('[data-del]').forEach(b => b.onclick = () => { collect(); delete cfg.groups[b.dataset.del]; drawGroups(); drawRules(); });
  document.querySelectorAll('[data-group]').forEach(c => c.onchange = () => { collect(); drawRules(); });
}
function drawRules() {
  const ls = labels(), senders = ['PC', ...ls, ...Object.keys(cfg.groups).map(g => '@' + g)];
  let h = '<thead><tr><th>Receiver</th><th>Everyone</th>' + senders.map(s => `<th>${esc(s)}</th>`).join('') + '</tr></thead><tbody>';
  h += '<tr><td>PC</td><td class="c">✔</td>' + senders.map(() => '<td class="c muted">✔</td>').join('') + '</tr>';
  for (const r of ls) {
    const rule = cfg.receive[r] || ['*'], all = rule.includes('*');
    h += `<tr><td>${esc(r)}</td><td class="c"><input type="checkbox" data-r="${esc(r)}" data-s="*" ${all ? 'checked' : ''} aria-label="${esc(r)} receives from everyone"></td>` +
      senders.map(s => s === r ? '<td class="c muted">own</td>' :
        `<td class="c"><input type="checkbox" data-r="${esc(r)}" data-s="${esc(s)}" ${!all && rule.includes(s) ? 'checked' : ''} ${all ? 'disabled' : ''} aria-label="${esc(r)} receives from ${esc(s)}"></td>`).join('') + '</tr>';
  }
  $('#rules').innerHTML = h + '</tbody>';
  document.querySelectorAll('#rules input').forEach(c => c.onchange = () => { collect(); drawRules(); });
}
function collect() {                       // the page -> cfg
  const devices = {};
  document.querySelectorAll('#seen input').forEach(i => {
    const l = i.value.trim(); if (l) (devices[l] = devices[l] || []).push(i.dataset.key);
  });
  cfg.devices = devices;
  const groups = {};
  Object.keys(cfg.groups).forEach(g => groups[g] = []);
  document.querySelectorAll('[data-group]:checked').forEach(c => groups[c.dataset.group].push(c.value));
  cfg.groups = groups;
  const receive = {};
  document.querySelectorAll('#rules input').forEach(c => {
    const r = c.dataset.r; receive[r] = receive[r] || [];
    if (c.checked && !c.disabled) receive[r].push(c.dataset.s);
  });
  for (const r of Object.keys(receive)) if (receive[r].includes('*')) receive[r] = ['*'];
  cfg.receive = receive;
}
$('#addgroup').onclick = () => { const g = $('#newgroup').value.trim(); if (g) { collect(); cfg.groups[g] = cfg.groups[g] || []; $('#newgroup').value = ''; drawGroups(); drawRules(); } };
$('#save').onclick = async () => {
  collect();
  const r = await fetch('config', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({devices: cfg.devices, groups: cfg.groups, receive: cfg.receive})});
  $('#msg').textContent = r.ok ? 'Saved.' : 'Not saved: ' + r.status;
  $('#msg').className = r.ok ? 'ok' : 'err';
};
fetch('config').then(r => r.json()).then(d => { cfg = d; drawSeen(); drawGroups(); drawRules(); });
</script></body></html>
"""
