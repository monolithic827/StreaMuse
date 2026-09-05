"""Resolves ffmpeg, cloudflared and go-librespot.

The exe ships all three, so nothing here runs for the people who download one. From a source
checkout ffmpeg and cloudflared are downloaded into the app's own bin folder instead, and
go-librespot is only ever looked for - see vendor/go-librespot/README.md."""

import asyncio
import os
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

USER_AGENT = "StreaMuse/1.0"


class DependencyManager:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._gate = asyncio.Lock()
        self.ffmpeg: str | None = None
        self.cloudflared: str | None = None

    @property
    def go_librespot(self) -> str | None:
        """Found, never downloaded: no go-librespot release carries the Windows pipe patch, so this
        is whatever the user built per vendor/go-librespot/README.md and put in BIN_DIR or on PATH.
        Resolving it live also means a binary dropped in needs no restart."""
        return resolve("go-librespot.exe")

    async def ensure_all(self) -> None:
        """Resolves every tool, downloading anything missing. Safe to call repeatedly."""
        async with self._gate:
            paths.BIN_DIR.mkdir(parents=True, exist_ok=True)
            self.ffmpeg = await self._ensure_ffmpeg()
            self.cloudflared = await self._ensure_single("cloudflared.exe", CLOUDFLARED_URL, "cloudflared")

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
    """The exe's own copy first, then our bin folder, then anywhere on PATH."""
    for directory in (paths.bundled_bin(), paths.BIN_DIR):
        if directory is not None and (directory / exe).is_file():
            return str(directory / exe)

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
