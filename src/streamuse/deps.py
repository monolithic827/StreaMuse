"""Resolves ffmpeg, cloudflared, go-librespot and yt-dlp, downloading whatever is missing into the
app's own bin folder.

The exe ships ffmpeg and cloudflared, so those two never download for the people who download one.
go-librespot always does: it is a patched build that nothing else distributes, published under its
own tag by .github/workflows/go-librespot.yml - see vendor/go-librespot/README.md. yt-dlp always
does too, and tracks latest rather than a pinned ref on purpose - its extractors break whenever
YouTube changes, and upstream ships releases at that cadence, so a fix must not need a StreaMuse
build to reach anyone."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import aiohttp

from . import jobs, paths
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
YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"

USER_AGENT = "StreaMuse/1.0"

CREATE_NO_WINDOW = 0x08000000
YT_DLP_UPDATE_TIMEOUT = 120


class DependencyManager:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._gate = asyncio.Lock()
        self.ffmpeg: str | None = None
        self.cloudflared: str | None = None
        self.yt_dlp: str | None = None

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
            ours = resolve("yt-dlp.exe")
            self.yt_dlp = await self._ensure_single("yt-dlp.exe", YT_DLP_URL, "yt-dlp")

            self._hub.set_dependencies([
                DependencyView("ffmpeg", self.ffmpeg),
                DependencyView("cloudflared", self.cloudflared),
                DependencyView("go-librespot", self.go_librespot),
                DependencyView("yt-dlp", self.yt_dlp),
            ])

            # Last, with the copy already usable and the list already published: the check is a
            # network round trip, and on a stalled connection it runs to its timeout. One downloaded
            # just now is the latest already, and one found on PATH is somebody else's install to
            # keep current.
            if ours and Path(ours).parent == paths.BIN_DIR:
                await self._update_yt_dlp(ours)

    async def _ensure_ffmpeg(self) -> str | None:
        existing = resolve("ffmpeg.exe")
        if existing:
            return existing

        target = paths.BIN_DIR / "ffmpeg.exe"
        self._hub.info("ffmpeg not found - downloading BtbN build (~100 MB)")
        archive = Path(tempfile.gettempdir()) / f"streamuse-ffmpeg-{os.getpid()}.zip"

        try:
            await self._download(FFMPEG_URL, archive, "ffmpeg")
            # Off the loop: it is seconds of unpacking, and the receiver that was started before
            # the downloads is already running on it.
            if not await asyncio.to_thread(_unpack_ffmpeg, archive, target):
                self._hub.error("ffmpeg archive did not contain bin/ffmpeg.exe")
                return None
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
            await asyncio.to_thread(_unpack_go_librespot, archive)
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

    async def _update_yt_dlp(self, path: str) -> None:
        """resolve() is satisfied by any copy at all, so without this the one downloaded on first
        launch is the one used forever. yt-dlp's own updater compares against the latest release
        and replaces the exe in place."""
        try:
            process = await asyncio.create_subprocess_exec(
                path, "-U",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self._hub.warn(f"yt-dlp update check failed: {exc}")
            return
        jobs.adopt(process)

        try:
            output, _ = await asyncio.wait_for(process.communicate(), YT_DLP_UPDATE_TIMEOUT)
        except TimeoutError:
            await jobs.end(process)
            self._hub.warn("yt-dlp update check failed: timed out")
            return

        lines = output.decode(errors="replace").strip().splitlines()
        last = lines[-1] if lines else f"exited {process.returncode}"
        if process.returncode != 0:
            self._hub.warn(f"yt-dlp update check failed: {last[:200]}")
        else:
            self._hub.info(last)

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


def _unpack_ffmpeg(archive: Path, target: Path) -> bool:
    with zipfile.ZipFile(archive) as zf:
        # The archive nests everything under ffmpeg-master-latest-win64-gpl/bin/.
        name = next((n for n in zf.namelist() if n.lower().endswith("bin/ffmpeg.exe")), None)
        if name is None:
            return False

        # resolve() trusts whatever sits at the final name, so it only appears there whole.
        partial = target.with_suffix(target.suffix + ".part")
        try:
            with zf.open(name) as source, partial.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return True


def _unpack_go_librespot(archive: Path) -> None:
    """Unpacked beside the bin folder's own files and moved in with the exe last: resolve() reads
    the exe's presence as the whole set being there, and an extraction that failed part-way left
    it in place with DLLs missing or cut short."""
    staging = paths.BIN_DIR / "go-librespot.part"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)
        for file in sorted(staging.iterdir(), key=lambda f: f.name.lower() == "go-librespot.exe"):
            file.replace(paths.BIN_DIR / file.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
