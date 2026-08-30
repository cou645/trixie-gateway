"""Firewall capability — iptables control (Linux)."""

import asyncio
import re


async def _ipt(*args, timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "iptables", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", "iptables: not found"


async def _ipt_save(timeout: int = 5) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "iptables-save",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace")
    except Exception:
        return ""


# ── read ──────────────────────────────────────────────────────────────────────

async def list_rules() -> dict:
    """Return all chains and their rules, parsed from iptables -L -n --line-numbers."""
    rc, out, err = await _ipt("-L", "-n", "--line-numbers", "-v")
    if rc != 0:
        return {"chains": [], "error": err.strip()}

    chains = []
    current = None
    for line in out.splitlines():
        # "Chain INPUT (policy ACCEPT 0 packets, 0 bytes)"
        m = re.match(r"^Chain\s+(\S+)\s+\(policy\s+(\w+)", line)
        if m:
            current = {"name": m.group(1), "policy": m.group(2), "rules": []}
            chains.append(current)
            continue
        # "Chain ts-input (1 references)"
        m = re.match(r"^Chain\s+(\S+)\s+\((\d+)\s+references\)", line)
        if m:
            current = {"name": m.group(1), "policy": None, "rules": []}
            chains.append(current)
            continue
        if current is None or line.startswith("num") or not line.strip():
            continue
        # Rule lines: "num pkts bytes target prot opt in out src dst [options]"
        m = re.match(
            r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(.*)",
            line,
        )
        if m:
            extra = m.group(10).strip()
            current["rules"].append({
                "num":    int(m.group(1)),
                "pkts":   m.group(2),
                "bytes":  m.group(3),
                "target": m.group(4),
                "proto":  m.group(5),
                "in":     m.group(7),
                "out":    m.group(8),
                "src":    m.group(9),
                "extra":  extra,
            })
    return {"chains": chains}


# ── write ─────────────────────────────────────────────────────────────────────

async def add_rule(
    chain: str,
    target: str,
    proto: str | None    = None,
    src: str | None      = None,
    dst: str | None      = None,
    dport: str | None    = None,
    sport: str | None    = None,
    iface_in: str | None = None,
    iface_out: str | None= None,
    insert: bool         = False,   # True = -I (top), False = -A (append)
    comment: str | None  = None,
) -> dict:
    cmd = ["-I" if insert else "-A", chain]
    if proto:       cmd += ["-p", proto]
    if src:         cmd += ["-s", src]
    if dst:         cmd += ["-d", dst]
    if iface_in:    cmd += ["-i", iface_in]
    if iface_out:   cmd += ["-o", iface_out]
    if dport:       cmd += ["--dport", str(dport)]
    if sport:       cmd += ["--sport", str(sport)]
    if comment:     cmd += ["-m", "comment", "--comment", comment]
    cmd += ["-j", target]

    rc, _, err = await _ipt(*cmd)
    return {"ok": rc == 0, "error": err.strip() or None}


async def delete_rule(chain: str, num: int) -> dict:
    rc, _, err = await _ipt("-D", chain, str(num))
    return {"ok": rc == 0, "error": err.strip() or None}


async def flush_chain(chain: str | None = None) -> dict:
    args = ["-F"] + ([chain] if chain else [])
    rc, _, err = await _ipt(*args)
    return {"ok": rc == 0, "error": err.strip() or None}


async def set_policy(chain: str, policy: str) -> dict:
    if policy not in ("ACCEPT", "DROP", "REJECT"):
        return {"ok": False, "error": f"invalid policy: {policy}"}
    rc, _, err = await _ipt("-P", chain, policy)
    return {"ok": rc == 0, "error": err.strip() or None}


# ── presets ───────────────────────────────────────────────────────────────────

PRESETS = {
    "open": {
        "description": "Accept all traffic (wide open)",
        "rules": [
            {"action": "policy", "chain": "INPUT",   "policy": "ACCEPT"},
            {"action": "policy", "chain": "FORWARD", "policy": "ACCEPT"},
            {"action": "policy", "chain": "OUTPUT",  "policy": "ACCEPT"},
            {"action": "flush"},
        ],
    },
    "lockdown": {
        "description": "Drop all inbound/forward except established + Tailscale",
        "rules": [
            {"action": "flush"},
            {"action": "policy", "chain": "INPUT",   "policy": "DROP"},
            {"action": "policy", "chain": "FORWARD", "policy": "DROP"},
            {"action": "policy", "chain": "OUTPUT",  "policy": "ACCEPT"},
            # allow loopback
            {"action": "add", "chain": "INPUT", "target": "ACCEPT", "iface_in": "lo"},
            # allow established/related
            {"action": "add", "chain": "INPUT",  "target": "ACCEPT",
             "extra_args": ["-m", "state", "--state", "ESTABLISHED,RELATED"]},
            # allow Tailscale interface
            {"action": "add", "chain": "INPUT", "target": "ACCEPT",
             "iface_in": "tailscale0"},
            # allow gateway port from Tailscale subnet
            {"action": "add", "chain": "INPUT", "target": "ACCEPT",
             "proto": "tcp", "src": "100.64.0.0/10", "dport": "8772"},
        ],
    },
    "gateway_only": {
        "description": "Allow only gateway port + Tailscale inbound",
        "rules": [
            {"action": "flush"},
            {"action": "policy", "chain": "INPUT",   "policy": "DROP"},
            {"action": "policy", "chain": "OUTPUT",  "policy": "ACCEPT"},
            {"action": "add", "chain": "INPUT", "target": "ACCEPT", "iface_in": "lo"},
            {"action": "add", "chain": "INPUT",  "target": "ACCEPT",
             "extra_args": ["-m", "state", "--state", "ESTABLISHED,RELATED"]},
            {"action": "add", "chain": "INPUT", "target": "ACCEPT",
             "iface_in": "tailscale0"},
        ],
    },
}


async def apply_preset(name: str) -> dict:
    preset = PRESETS.get(name)
    if not preset:
        return {"ok": False, "error": f"unknown preset: {name}. Available: {list(PRESETS)}"}

    errors = []
    for rule in preset["rules"]:
        action = rule["action"]
        if action == "flush":
            r = await flush_chain(rule.get("chain"))
        elif action == "policy":
            r = await set_policy(rule["chain"], rule["policy"])
        elif action == "add":
            extra = rule.get("extra_args", [])
            # build args manually for preset entries with extra_args
            cmd = ["-A", rule["chain"]]
            if rule.get("proto"):    cmd += ["-p", rule["proto"]]
            if rule.get("src"):      cmd += ["-s", rule["src"]]
            if rule.get("iface_in"): cmd += ["-i", rule["iface_in"]]
            if rule.get("dport"):    cmd += ["--dport", str(rule["dport"])]
            cmd += extra
            cmd += ["-j", rule["target"]]
            rc, _, err = await _ipt(*cmd)
            r = {"ok": rc == 0, "error": err.strip() or None}
        else:
            continue
        if not r.get("ok"):
            errors.append(r.get("error", "unknown error"))

    return {"ok": len(errors) == 0, "preset": name, "errors": errors or None}
