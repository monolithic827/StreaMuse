"""Resolves ffmpeg, cloudflared and go-librespot, downloading whatever is missing into the app's own
bin folder.

The exe ships ffmpeg and cloudflared, so those two never download for the people who download one.
go-librespot always does: it is a patched build that nothing else distributes, published under its
own tag by .github/workflows/go-librespot.yml - see vendor/go-librespot/README.md."""

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
#: The upstream release the pipe patch applies to. LIBRESPOT_REF in the workflow that builds and
#: publishes the asset must name the same one.
GO_LIBRESPOT_REF = "v0.9.0"
GO_LIBRESPOT_URL = (
    "https://github.com/monolithic827/StreaMuse/releases/download/"
    f"go-librespot-{GO_LIBRESPOT_REF}/go-librespot-win-x64.zip"
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
        """Resolved live rather than cached like the other two, so the Spotify receiver sees it the
        moment the download below lands - the source is selected before ensure_all runs."""
        return resolve("go-librespot.exe")

    async def ensure_all(self) -> None:
        """Resolves every tool, downloading anything missing. Safe to call repeatedly."""
        async with self._gate:
            paths.BIN_DIR.mkdir(parents=True, exist_ok=True)
            # First, because it is the only one a receiver waits on and the smallest by far: behind
            # ffmpeg it would be minutes before Spotify could be picked.
            await self._ensure_go_librespot()
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

    async def _ensure_go_librespot(self) -> None:
        """The archive carries the exe together with the audio DLLs CGO links it against, so a
        machine that has never seen MSYS2 gets a working set or none at all."""
        if self.go_librespot is not None:
            return

        archive = Path(tempfile.gettempdir()) / f"streamuse-go-librespot-{os.getpid()}.zip"
        self._hub.info("go-librespot not found - downloading")

        try:
            await self._download(GO_LIBRESPOT_URL, archive, "go-librespot")
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(paths.BIN_DIR)
        except Exception as exc:
            self._hub.error(f"go-librespot download failed: {exc}")
            return
        finally:
            archive.unlink(missing_ok=True)

        self._hub.info(f"go-librespot installed to {paths.BIN_DIR}")

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
