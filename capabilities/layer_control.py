"""Layer control capability — list AUFS branches (Fatdog64).

Fatdog64 builds its root with AUFS union branches:
  pup_init   — initial tmpfs (ro, always active)
  kernel-modules — kernel squashfs (ro, always active)
  pup_ro     — base Fatdog64 squashfs (ro, always active)
  pup_ro*    — extra SFS packages loaded at boot (ro)
  devbase    — sda2 ext4 base overlay (rw)
  devsave    — sda2 ext4 dev save (rw)
  pup_save   — sda2 ext4 persistent save file (rw, always active)

"Toggling" a layer means adding it to the boot SFS list in
/etc/BOOTSTATE or /etc/rc.d/rc.local (written as note for next reboot).
"""

from pathlib import Path
import re

_LOCKED = frozenset({"pup_init", "kernel-modules", "pup_ro", "pup_save",
                     "devbase", "devsave", "pup_ro10"})

_AUFS_ROOT = Path("/aufs")
_BOOTSTATE = Path("/etc/BOOTSTATE")
_SFS_DIR   = Path("/aufs")        # where branch points live

# /proc/mounts format: <dev> <mountpoint> <fstype> <opts> <dump> <pass>
_BRANCH_RE = re.compile(
    r"^(?P<dev>\S+)\s+(?P<mp>/aufs/\S+)\s+(?P<fs>\S+)\s+(?P<opts>\S+)"
)


def list_layers(sda2: Path) -> list[dict]:
    """Return a live list of AUFS branches parsed from /proc/mounts."""
    wanted = _desired(sda2)
    live   = _live_branches()
    layers = []
    for branch in live:
        name   = branch["name"]
        layers.append({
            **branch,
            "locked":    name in _LOCKED,
            "wanted":    name in wanted,
        })
    # also show desired-but-not-yet-active extras
    active_names = {b["name"] for b in live}
    for name in sorted(wanted - active_names):
        layers.append({
            "name":        name,
            "description": "pending (next boot)",
            "fs":          "unknown",
            "mode":        "ro",
            "active":      False,
            "locked":      False,
            "wanted":      True,
        })
    return layers


def set_layer(sda2: Path, name: str, enable: bool) -> dict:
    if name in _LOCKED:
        raise ValueError(f"'{name}' is a core branch and cannot be toggled")
    wanted = _desired(sda2)
    if enable:
        wanted.add(name)
    else:
        wanted.discard(name)
    _write_desired(sda2, wanted)
    return {"ok": True, "name": name, "active": enable, "note": "reboot to apply"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _desired_file(sda2: Path) -> Path:
    return sda2 / "fd64" / "layers" / "desired"


def _desired(sda2: Path) -> set:
    f = _desired_file(sda2)
    if not f.exists():
        return set()
    return {
        line.strip()
        for line in f.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def _write_desired(sda2: Path, desired: set):
    f = _desired_file(sda2)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(sorted(desired)) + "\n")


def _live_branches() -> list[dict]:
    """Parse /proc/mounts for AUFS branches."""
    branches = []
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return branches

    for line in mounts:
        m = _BRANCH_RE.match(line)
        if not m:
            continue
        mp   = m.group("mp")           # e.g. /aufs/pup_ro
        fs   = m.group("fs")           # squashfs, ext4, tmpfs
        opts = m.group("opts")
        name = mp.removeprefix("/aufs/")
        mode = "ro" if "ro," in opts or opts.endswith(",ro") or opts == "ro" else "rw"
        branches.append({
            "name":        name,
            "description": _describe(name, fs),
            "fs":          fs,
            "mountpoint":  mp,
            "mode":        mode,
            "active":      True,
            "locked":      name in _LOCKED,
        })
    return branches


def _describe(name: str, fs: str) -> str:
    _DESC = {
        "pup_init":       "Initial tmpfs (boot base)",
        "kernel-modules": "Kernel modules squashfs (initrd)",
        "pup_ro":         "Base Fatdog64 squashfs",
        "pup_ro10":       "Kernel modules SFS (external, alt to /aufs/kernel-modules)",
        "devbase":        "sda2 base overlay (ext4)",
        "devsave":        "sda2 dev save (ext4)",
        "pup_save":       "Persistent save file (ext4)",
    }
    if name in _DESC:
        return _DESC[name]
    if name.startswith("pup_ro"):
        return f"Extra SFS package ({fs})"
    return f"AUFS branch ({fs})"
