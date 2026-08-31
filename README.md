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
and **enabled** (starts on boot — see "Known snag" below for the history).

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

## X11 auth for the systemd unit (fixed 2026-08-31)

`webrtc_screen.py`'s x11grab capture needs `DISPLAY`/`XAUTHORITY` --
systemd doesn't inherit the interactive X session's environment the way
a login shell does, so without these set explicitly, every
`POST /yay/webrtc/offer` failed with `av.error.OSError: Input/output
error` (`Authorization required, but no authorization protocol
specified` in the log). Manual runs (`DISPLAY=:0 python gateway.py`)
never hit this, since they inherit a working X auth from whatever shell
started them -- only the systemd-launched process was affected. The
unit now sets both explicitly:

```
Environment=DISPLAY=:0
Environment=XAUTHORITY=/root/.Xauthority
```

Verified against the live restarted service: two real WebRTC clients
over actual HTTP (`/yay/webrtc/offer` → `/yay/webrtc/answer`) both
received real decoded frames (953/303 over 4s), `/yay/webrtc/peers`
tracked both correctly, and closing one at a time released the shared
video/audio capture cleanly with no errors in the log.

## Known snag (2026-08-29) — resolved 2026-08-31

The systemd unit was **installed but disabled** because an earlier draft had
`ExecStartPre=… fuser -k 8772/tcp`; `fuser` wedged in **`D` state inside TOMOYO
LSM** (`tomoyo_write_file`, learning-table saturated, no `tomoyo-queryd` running)
and was unkillable, stuck in the service's cgroup — so systemd entered *failed
mode* on every start.

The current unit has **no `fuser`**, and is now `systemctl enable`d
(`enabled`/`active`, survives reboot via `multi-user.target`) — confirmed via
`systemctl is-enabled`/`is-active` after a fresh `enable` + restart, no TOMOYO
issue hit.

Avoid `fuser` on this box generally while TOMOYO learning is saturated.
