# PyInstaller spec — one self-contained gateway binary per platform.
#
#   pyinstaller trixie-gateway.spec          (run on the target OS)
#
# PyInstaller is not a cross-compiler: a Windows .exe must be built on Windows
# and a macOS .app on macOS. .github/workflows/build-gateway.yml does exactly
# that on GitHub's runners, which is how the Windows/macOS builds get produced
# without owning either machine.
#
# What needs help here:
#   * platforms.windows / platforms.macos are imported dynamically by OS, so
#     PyInstaller's static analysis never sees the one for the host — they are
#     added as hidden imports (and the other two are excluded, so a Linux build
#     doesn't try to bundle pycaw/pyobjc).
#   * av and aiortc carry native libraries (ffmpeg, libsrtp, libvpx/openh264);
#     collect_dynamic_libs picks those up.
#   * providers/ and capabilities/ are imported by name in places, so they are
#     collected wholesale rather than relied on being traced.

import sys
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

binaries = []
for pkg in ("av", "aiortc", "pylibsrtp", "cryptography"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

hiddenimports = [
    "aiohttp", "aiortc", "av", "pylibsrtp", "cryptography", "aioice",
    "dns", "ifaddr", "pyee",
]
hiddenimports += collect_submodules("capabilities")
hiddenimports += collect_submodules("providers")

# Only the host's backend: the others' dependencies don't exist here.
excludes = ["tkinter", "PyQt5", "PyQt6", "PySide6", "matplotlib", "numpy.testing"]
if IS_WIN:
    hiddenimports += ["platforms.windows", "pycaw", "comtypes", "psutil"]
    excludes += ["platforms.macos"]
elif IS_MAC:
    hiddenimports += ["platforms.macos", "Quartz", "AppKit", "psutil"]
    excludes += ["platforms.windows"]
else:
    excludes += ["platforms.windows", "platforms.macos"]

a = Analysis(
    ["gateway.py"],
    pathex=["."],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="trixie-gateway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries trip antivirus heuristics
    console=True,       # it's a server: users need to see the log
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if IS_MAC:
    # A .app bundle is what macOS asks for permissions on behalf of, so the
    # Screen Recording / Accessibility grants stick to the app rather than to
    # whichever terminal launched it.
    app = BUNDLE(
        exe,
        name="TrixieGateway.app",
        icon=None,
        bundle_identifier="net.chameleon-ai-agent.trixie-gateway",
        info_plist={
            "CFBundleName": "Trixie Gateway",
            "CFBundleDisplayName": "Trixie Gateway",
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
            # Shown in the permission prompts macOS raises on first use.
            "NSCameraUsageDescription":
                "Screen sharing to your phone uses the AVFoundation capture device.",
            "NSMicrophoneUsageDescription":
                "Streams this Mac's audio to your phone when audio sharing is on.",
            "NSAppleEventsUsageDescription":
                "Controls windows and system volume on your behalf.",
        },
    )
