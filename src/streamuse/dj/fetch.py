"""Turns a free-text request into decoded PCM and the tags that go with it. Searches
music.youtube.com rather than youtube.com: the music service answers with track, artist and album
as separate fields and square cover art, where the video site gives a title like "... (Official
Music Video Remastered)" and a 16:9 screenshot. Downloading arbitrary tracks this way is against
YouTube's ToS.

Runs on a detached background task (see DjMixer.request) with no cancellation tied to the HTTP
request that triggered it - tying it to the request-abort token killed the subprocesses mid-download
the first time this was wired naively."""

import asyncio
import subprocess
import tempfile
import urllib.parse
import uuid
from pathlib import Path

import aiohttp
import numpy as np

from .. import jobs

CREATE_NO_WINDOW = 0x08000000
FETCH_TIMEOUT = 180
SEARCH_RESULTS = 6
SAMPLE_RATE = 44100

#: Unlikely enough in a title to split fields on.
SEPARATOR = " |#| "


class FetchedTrack:
    def __init__(self, stereo: np.ndarray, title: str, artist: str, album: str,
                artwork: bytes | None) -> None:
        self.stereo = stereo
        self.title = title
        self.artist = artist
        self.album = album
        self.artwork = artwork


class SearchResult:
    def __init__(self, video_id: str, title: str, artist: str, thumbnail: str) -> None:
        self.video_id = video_id
        self.title = title
        self.artist = artist
        self.thumbnail = thumbnail


class YtDlpFetcher:
    def __init__(self, deps, hub) -> None:
        self._deps = deps
        self._hub = hub

    async def search(self, query: str) -> list[SearchResult]:
        """Lists the top candidates for a query without downloading anything - --flat-playlist skips
        the per-entry lookup --print would otherwise trigger, which is what keeps this fast enough
        to run while someone's still typing."""
        if self._deps.yt_dlp is None:
            raise RuntimeError("yt-dlp is not available - check the Dependencies panel")

        process = await asyncio.create_subprocess_exec(
            self._deps.yt_dlp,
            "--flat-playlist", "--quiet", "--no-warnings",
            "--playlist-items", f"1-{SEARCH_RESULTS}",
            "--print", SEPARATOR.join((
                "%(id)s", "%(track,title)s", "%(artist,uploader,channel)s", "%(thumbnail)s")),
            "https://music.youtube.com/search?q=" + urllib.parse.quote(query),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(process)

        try:
            out, err = await asyncio.wait_for(process.communicate(), FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("yt-dlp search timed out")

        if process.returncode != 0:
            raise RuntimeError(f"yt-dlp search failed: {_tail(err)}")

        return _parse_search_lines(out)

    async def fetch(self, query: str, video_id: str | None = None) -> FetchedTrack:
        if self._deps.yt_dlp is None or self._deps.ffmpeg is None:
            raise RuntimeError("yt-dlp or ffmpeg is not available - check the Dependencies panel")

        workdir = Path(tempfile.gettempdir())
        target = workdir / f"streamuse-dj-{uuid.uuid4().hex}"

        # A video_id from search() names the exact track picked from the dropdown; without one this
        # falls back to the same first-result search fetch() always did.
        source = (f"https://music.youtube.com/watch?v={video_id}" if video_id else
                 "https://music.youtube.com/search?q=" + urllib.parse.quote(query))

        process = await asyncio.create_subprocess_exec(
            self._deps.yt_dlp,
            "-f", "bestaudio", "--no-playlist", "--playlist-items", "1",
            # --print implies --simulate unless --no-simulate is also passed, or nothing downloads.
            "--no-simulate", "--quiet", "--no-warnings",
            "-o", f"{target}.%(ext)s",
            "--print", SEPARATOR.join((
                "%(track,title)s", "%(artist,uploader)s", "%(album)s", "%(thumbnail)s")),
            source,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(process)

        try:
            out, err = await asyncio.wait_for(process.communicate(), FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("yt-dlp timed out")

        downloaded = next(iter(workdir.glob(f"{target.name}.*")), None)
        if process.returncode != 0 or downloaded is None:
            raise RuntimeError(f"yt-dlp failed: {_tail(err)}")

        title, artist, album, thumbnail_url = _parse_print_line(out, query)

        try:
            stereo = await self._decode(downloaded)
        finally:
            downloaded.unlink(missing_ok=True)

        artwork = await self._fetch_thumbnail(thumbnail_url) if thumbnail_url else None
        return FetchedTrack(stereo, title, artist, album, artwork)

    async def _decode(self, path: Path) -> np.ndarray:
        process = await asyncio.create_subprocess_exec(
            self._deps.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(process)

        try:
            raw, err = await asyncio.wait_for(process.communicate(), FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("decode timed out")

        if not raw:
            raise RuntimeError(f"decode produced no audio: {_tail(err)}")

        floats = np.frombuffer(raw, dtype="<f4")
        usable = floats.size - floats.size % 2
        return floats[:usable].reshape(-1, 2).copy()

    async def _fetch_thumbnail(self, url: str) -> bytes | None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url) as reply:
                    if reply.status != 200:
                        return None
                    return await reply.read()
        except aiohttp.ClientError:
            return None


def _parse_print_line(out: bytes, fallback_title: str) -> tuple[str, str, str, str | None]:
    text = out.decode("utf-8", "replace").strip()
    line = next((l for l in reversed(text.splitlines()) if SEPARATOR in l), "")
    fields = line.split(SEPARATOR) if line else []

    def clean(index: int) -> str | None:
        value = fields[index].strip() if index < len(fields) else ""
        return None if not value or value == "NA" else value

    return (clean(0) or fallback_title, clean(1) or "", clean(2) or "", clean(3))


def _parse_search_lines(out: bytes) -> list[SearchResult]:
    results = []
    for line in out.decode("utf-8", "replace").splitlines():
        fields = line.split(SEPARATOR)
        if len(fields) != 4:
            continue

        def clean(index: int) -> str:
            value = fields[index].strip()
            return "" if value == "NA" else value

        video_id = clean(0)
        if not video_id:
            continue
        results.append(SearchResult(video_id, clean(1) or "Unknown title", clean(2), clean(3)))

    return results


def _tail(stream: bytes, limit: int = 300) -> str:
    text = stream.decode("utf-8", "replace").strip()
    return text if len(text) <= limit else text[-limit:]
