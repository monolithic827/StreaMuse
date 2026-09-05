"""Resolves ffmpeg, cloudflared and go-librespot, downloading release builds into the app's
own bin folder when they are not already there or on PATH."""

import asyncio
import os
import tarfile
import tempfile
import zipfile
from pathlib import Path

import aiohttp

from . import paths
from .state import DependencyView

FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-windows-amd64.exe"
)

#: go-librespot's own releases carry a Windows build whose pipe backend is a stub, so this is our
#: fork's build of the same version with the named-pipe output patched in. Swap to the upstream
#: asset once a release ships the patch.
GO_LIBRESPOT_URL = (
    "https://github.com/monolithic827/streamuse/releases/download/"
    "go-librespot-0.9.0-winpipe/go-librespot_windows_amd64.tar.gz"
)

USER_AGENT = "StreaMuse/1.0"


class DependencyManager:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._gate = asyncio.Lock()
        self.ffmpeg: str | None = None
        self.cloudflared: str | None = None
        self.go_librespot: str | None = None

    async def ensure_all(self) -> None:
        """Resolves every tool, downloading anything missing. Safe to call repeatedly."""
        async with self._gate:
            paths.BIN_DIR.mkdir(parents=True, exist_ok=True)
            self.ffmpeg = await self._ensure_ffmpeg()
            self.cloudflared = await self._ensure_single("cloudflared.exe", CLOUDFLARED_URL, "cloudflared")
            self.go_librespot = await self._ensure_go_librespot()

            self._hub.set_dependencies([
                DependencyView("ffmpeg", self.ffmpeg),
                DependencyView("cloudflared", self.cloudflared),
                DependencyView("go-librespot", self.go_librespot),
            ])

    async def _ensure_ffmpeg(self) -> str | None:
        existing = resolve("ffmpeg.exe")
        if existing:
            return existing

        target = paths.BIN_DIR / "ffmpeg.exe"
        self._hub.info("ffmpeg not found - downloading BtbN build (~100 MB)")
        archive = Path(tempfile.gettempdir()) / f"streamuse-ffmpeg-{os.getpid()}.zip"

        try:
            await self._download(FFMPEG_URL, archive, "ffmpeg")
            with zipfile.ZipFile(archive) as zf:
                # The archive nests everything under ffmpeg-master-latest-win64-gpl/bin/.
                name = next(
                    (n for n in zf.namelist() if n.lower().endswith("bin/ffmpeg.exe")), None)
                if name is None:
                    self._hub.error("ffmpeg archive did not contain bin/ffmpeg.exe")
                    return None
                target.write_bytes(zf.read(name))
        except Exception as exc:
            self._hub.error(f"ffmpeg download failed: {exc}")
            return None
        finally:
            archive.unlink(missing_ok=True)

        self._hub.info(f"ffmpeg installed to {target}")
        return str(target)

    async def _ensure_go_librespot(self) -> str | None:
        existing = resolve("go-librespot.exe")
        if existing:
            return existing

        target = paths.BIN_DIR / "go-librespot.exe"
        self._hub.info("go-librespot not found - downloading (~5 MB)")
        archive = Path(tempfile.gettempdir()) / f"streamuse-librespot-{os.getpid()}.tar.gz"

        try:
            await self._download(GO_LIBRESPOT_URL, archive, "go-librespot")
            with tarfile.open(archive) as tf:
                member = next(
                    (m for m in tf.getmembers() if m.name.lower().endswith("go-librespot.exe")), None)
                if member is None:
                    self._hub.error("go-librespot archive did not contain go-librespot.exe")
                    return None
                source = tf.extractfile(member)
                if source is None:
                    self._hub.error("go-librespot archive entry could not be read")
                    return None
                target.write_bytes(source.read())
        except Exception as exc:
            self._hub.error(f"go-librespot download failed: {exc}")
            return None
        finally:
            archive.unlink(missing_ok=True)

        self._hub.info(f"go-librespot installed to {target}")
        return str(target)

    async def _ensure_single(self, exe: str, url: str, label: str) -> str | None:
        existing = resolve(exe)
        if existing:
            return existing

        target = paths.BIN_DIR / exe
        self._hub.info(f"{label} not found - downloading")

        try:
            await self._download(url, target, label)
        except Exception as exc:
            self._hub.error(f"{label} download failed: {exc}")
            return None

        self._hub.info(f"{label} installed to {target}")
        return str(target)

    async def _download(self, url: str, destination: Path, label: str) -> None:
        partial = destination.with_suffix(destination.suffix + ".part")
        timeout = aiohttp.ClientTimeout(total=15 * 60)

        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers={"User-Agent": USER_AGENT}
            ) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    total = response.content_length or 0
                    read = 0
                    reported = -1

                    with partial.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(1 << 16):
                            handle.write(chunk)
                            read += len(chunk)
                            if total <= 0:
                                continue
                            percent = read * 100 // total
                            if percent >= reported + 10:
                                reported = percent - percent % 10
                                self._hub.info(f"{label} download {reported}%")

            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)


def resolve(exe: str) -> str | None:
    """Look in our own bin folder first, then anywhere on PATH."""
    local = paths.BIN_DIR / exe
    if local.is_file():
        return str(local)

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        try:
            candidate = Path(directory.strip('"')) / exe
            if candidate.is_file():
                return str(candidate)
        except (OSError, ValueError):
            # PATH can carry entries with characters that are not valid in a path; skip them.
            continue

    return None
