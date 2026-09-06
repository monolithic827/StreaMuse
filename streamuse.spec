# PyInstaller build: one windowed StreaMuse.exe.
#
# wwwroot is data, not code, so it is added explicitly and read through paths.wwwroot() - which
# resolves the same from a source checkout and from the unpacked bundle.
#
# ffmpeg, cloudflared and go-librespot are shipped inside the exe so a download works offline and
# on first launch: CI stages them into vendor/bin (see .github/workflows/build.yml) and
# paths.bundled_bin() is where deps.resolve looks first. They go in as datas rather than binaries
# because they are standalone programs, not libraries to be scanned for dependencies. A local build
# with nothing staged still produces a working exe - it just falls back to downloading them.

from pathlib import Path

staged = sorted(Path("vendor/bin").glob("*.exe"))

analysis = Analysis(
    ["src/streamuse/__main__.py"],
    pathex=["src"],
    datas=[("src/streamuse/wwwroot", "streamuse/wwwroot"), *((str(p), "bin") for p in staged)],
    hiddenimports=["streamuse.selftest"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="StreaMuse",
    console=False,
    onefile=True,
    upx=False,
    strip=False,
)
