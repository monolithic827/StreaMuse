# PyInstaller build: one windowed StreaMuse.exe.
#
# wwwroot is data, not code, so it is added explicitly and read through paths.wwwroot() - which
# resolves the same from a source checkout and from the unpacked bundle. ffmpeg, cloudflared and
# go-librespot are still downloaded on first run rather than shipped.

analysis = Analysis(
    ["src/streamuse/__main__.py"],
    pathex=["src"],
    datas=[("src/streamuse/wwwroot", "streamuse/wwwroot")],
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
