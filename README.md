# trixie-gateway

AI + system-control gateway daemon — the **PC side of the `yayos-apk` phone app**.
Ported from `../fd64-gateway` (the Fatdog64 fork, kept as the frozen reference for
the `YaYOS-gateway-v2.sfs` bundle) to run on this Debian Trixie box.

OpenAI-compatible `/v1/chat/completions` (routes to Anthropic / OpenAI / local) +
~120 `/yay/*` endpoints: system info, AUFS layer control, brightness, audio,
network / Wi-Fi, media, window manager, lockscreen, firewall, Bluetooth, chat
history, apps, terminal exec, WebRTC screen stream, desktop input, semantic
journal, capability tokens. Listens on a Unix socket (local tools) + TCP `:8772`
(phone / LAN / Tailscale).

## What changed from fd64-gateway

| | fd64-gateway | trixie-gateway |
|---|---|---|
| init | SysV `fd64-gateway.init` | `trixie-gateway.service` (systemd) |
| python | system `python3` | `/root/pyqt6-venv` (system 3.13 has no aiohttp) |
| Wi-Fi iface | hard-coded `wlan2` | auto-detected (`_detect_wifi_iface()`, `wlp0s20f3` here) |
| config | `~/.yayos/fd64-gateway.json` | `~/.config/trixie-gateway/config.json` (legacy `~/.yayos/*` still read) |
| data dirs | `~/.yayos/` (key, journal, chat, media history) | `~/.config/trixie-gateway/` |
| DHCP | `udhcpc` → `dhclient` | `dhcpcd` → `udhcpc` → `dhclient` |
| SIGTERM | raised `RuntimeError`, exit 1 | clean exit 0 (systemd-friendly) |
| version/platform reported | `0.1.0-fd64` / `fatdog64` / `sysv` | `0.1.0-trixie` / `debian-trixie` / `systemd` |
| `/mnt/sda2` | — | resolves to `/aufs/devbase` when sda2 absent |

AUFS layer control is unchanged — this box still stacks with AUFS (7 mounts).

## Install / run

```sh
# deps (once)
/root/pyqt6-venv/bin/pip install -r requirements.txt

# run directly
/root/pyqt6-venv/bin/python /aufs/devbase/trixie-gateway/gateway.py --port 8772

# or as a service
cp trixie-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trixie-gateway
curl -s http://127.0.0.1:8772/yay/health
```

The unit is already installed at `/etc/systemd/system/trixie-gateway.service`
(currently **disabled** — see "Known snag" below).

Phone app gateway URL: `http://<tailscale0-ip>:8772` (`ip addr show tailscale0`).

## Config

`~/.config/trixie-gateway/config.json` (mode 0600), created here with a DeepSeek
provider from `/aufs/devbase/chameleon/api_keys.env`:

```json
{
  "capabilities": { "allow_unauthenticated": true },
  "providers": {
    "<name>": { "api_key": "...", "url": "https://…", "default_model": "…" }
  }
}
```

Add / change providers by editing the file (then `systemctl restart trixie-gateway`)
or `PUT /yay/providers`. **Rotate the keys** — the fd64.sfs copies
(`~/.yayos/gateway.json`, `fd64-gateway.json`) held live Anthropic / OpenAI /
DeepSeek / NVIDIA keys; redacted structure is in `../fd64-gateway/porting/`.

## Audio streaming to the phone (optional)

Off by default. To enable the WebRTC audio tee:
1. Install `porting/99-yayos.conf` → `/etc/modprobe.d/`, `porting/99-yayos-fdtee.conf`
   → `/etc/alsa/conf.d/`, and change `hw:Audio,0` in it to this box's card (`aplay -l`).
2. Trixie runs PipeWire/Pulse — a raw-ALSA `hw:` tee may fight the sound server;
   test, or do the tee at the PipeWire layer.
3. Uncomment `Environment=ALSA_PCMOUT=fdtee` in `trixie-gateway.service`.

## Known snag (2026-08-29)

The systemd unit is **installed but disabled**. An earlier draft of the unit had
`ExecStartPre=… fuser -k 8772/tcp`; `fuser` wedged in **`D` state inside TOMOYO
LSM** (`tomoyo_write_file`, learning-table saturated, no `tomoyo-queryd` running)
and is unkillable, stuck in the service's cgroup — so systemd enters *failed
mode* on every start. **This clears on reboot.** After a reboot:

```sh
systemctl enable --now trixie-gateway
```

The current unit has **no `fuser`**. The daemon itself is verified working
(standalone: `/yay/health`, `/yay/system`, `/yay/network/interfaces` all 200,
clean SIGTERM). Until reboot, run it directly with the venv command above.

Avoid `fuser` on this box generally while TOMOYO learning is saturated.
