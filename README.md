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

## Windows / macOS (cross-platform gateway)

The phone app talks to this gateway over plain HTTP + WebRTC, neither of which
is Linux-specific — only the capabilities that touch the desktop are. Those now
sit behind `platforms/`:

| | Linux | Windows | macOS |
|---|---|---|---|
| screen capture | ffmpeg `x11grab` | ffmpeg `gdigrab` | ffmpeg `avfoundation` |
| mouse / keyboard | `xdotool` | `user32` SendInput (ctypes) | Quartz `CGEvent` (pyobjc) |
| window list / focus / close | `wmctrl` + `xdotool` | `user32` EnumWindows | `CGWindowListCopyWindowInfo` |
| volume / mute | `amixer` (ALSA) | Core Audio via `pycaw` | `osascript` |
| system info | procfs / sysfs | `psutil` | `psutil` |
| terminal | `/bin/sh` | PowerShell, else `cmd.exe` | `$SHELL -lc` |

On Linux `platforms.backend` is `None` and every capability runs its original
code path — the port adds no dependency and changes no behaviour there.

**Porting status.** Supported everywhere: system, desktop (screen + input),
windows, audio, terminal, chat, journal. Still Linux-only: layers (aufs has no
Windows/macOS analogue), firewall (iptables), bluetooth (bluetoothctl),
brightness (sysfs), network (nmcli), media (Chameleon Media Center). Those
routes answer `501` with `{"unsupported": true}` off Linux rather than failing
somewhere deep inside a missing tool.

`GET /yay/capabilities` reports the OS, the feature matrix and a runtime probe
(missing packages, ungranted macOS permissions). `GET /yay/health` now carries
`os` and `features` too, so the app can hide what a given host can't do.

### Run it

```bash
# both platforms, from the repo
pip install -r requirements.txt -r requirements-crossplatform.txt
python gateway.py --port 8772
```

ffmpeg's capture backends come with PyAV, so no separate ffmpeg install is
needed for the screen stream.

**macOS permissions.** macOS gates both things this gateway needs, and neither
can be granted from code — grant them to whichever app runs the process
(Terminal, iTerm, or a packaged build), in System Settings > Privacy & Security:

* **Accessibility** — without it `CGEventPost` silently does nothing, so the
  mouse and keyboard appear connected but dead.
* **Screen Recording** — without it the video stream is black.

`/yay/capabilities` reports both, so check it first when something looks broken.
If the stream is black even with permission granted, the avfoundation screen
index is wrong (it sits after the cameras, and varies per Mac): list them with
`ffmpeg -f avfoundation -list_devices true -i ""` and set
`TRIXIE_AVF_SCREEN_INDEX`.

**Windows notes.** The process sets per-monitor DPI awareness at import,
without which clicks land offset on a scaled display. `gdigrab` cannot capture
some hardware-accelerated fullscreen apps (games, protected video); those
appear black. Volume control needs `pycaw` — without it the audio routes report
unsupported rather than silently doing nothing.

**Untested on real hardware.** Written against the Win32/Quartz APIs but not
yet run on a Windows or macOS machine; expect to shake out small issues on
first run, starting with `/yay/capabilities`.

## Building standalone binaries (PyInstaller)

`trixie-gateway.spec` produces one self-contained executable per platform —
no Python, no pip, no venv on the target machine:

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm trixie-gateway.spec
# -> dist/trixie-gateway          (Linux)
# -> dist/trixie-gateway.exe      (Windows)
# -> dist/TrixieGateway.app       (macOS)
```

PyInstaller is **not** a cross-compiler: each binary must be built on its own
OS. `.github/workflows/build-gateway.yml` builds all four targets (Linux,
Windows, macOS Intel, macOS Apple silicon) on GitHub runners, smoke-tests each
one by starting it and calling `/yay/health` + `/yay/capabilities`, and
attaches them to the release when the workflow runs from a `v*` tag. That is
the supported way to produce Windows/macOS builds without owning the hardware.

The macOS target is a `.app` bundle on purpose: macOS attaches Screen Recording
and Accessibility permissions to the bundle, so the grant sticks to the app
instead of to whichever terminal launched it.

**What bundling does and doesn't do.** It stops casual editing — there is no
`.py` to open and change, and it removes the "install Python first" barrier.
It is not encryption: the bundle still contains Python bytecode, which
determined users can extract and decompile. If the goal is to protect
commercial logic, the only real answer is to keep that logic server-side (or
rewrite the sensitive parts in a compiled language); treat the binary as a
distribution format, not as a licence enforcement mechanism.

**UPX is deliberately off** in the spec: packed binaries trip antivirus
heuristics, and an unsigned .exe already has enough of a SmartScreen problem.
Signing (Authenticode on Windows, notarisation on macOS) needs paid
certificates and is not wired up here; without it users see a warning they must
click past on first run.
