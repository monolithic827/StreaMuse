"""Downloads a resolved track to a local file before it plays.

Decoding then reads an ordinary file instead of an active network connection, so a CDN reset during
this step only costs download time - ffmpeg's own `-reconnect` keeps retrying against a target with
no realtime deadline to miss, unlike the same reset landing mid-playback against the paced decoder
(see decoder.py and CLAUDE.md's "audio buffer overran" note).

Matroska holds whatever codec the source used (opus, aac, ...) via `-c:a copy`, so the output
container never has to branch on that - it is always `.mka` regardless of the source.
"""

import asyncio
import contextlib
import subprocess
from pathlib import Path
from uuid import uuid4

from ... import jobs, paths

CREATE_NO_WINDOW = 0x08000000


def new_path() -> Path:
    """A random name rather than one derived from the track: the same track can be queued twice and
    each occurrence gets its own independent download and its own independent discard."""
    return paths.YTDLP_CACHE_DIR / f"{uuid4().hex}.mka"


def discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def clear_all() -> None:
    """Run once at startup - a file left behind by a crash or a killed process would otherwise sit
    in the cache dir forever, since nothing else ever revisits an old path."""
    for file in paths.YTDLP_CACHE_DIR.glob("*.mka"):
        discard(file)


async def download(ffmpeg_path: str, stream_url: str, http_headers: dict[str, str], dest: Path) -> None:
    arguments = [
        "-hide_banner", "-nostdin", "-y", "-loglevel", "level+warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
    ]
    if http_headers:
        arguments += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())]
    arguments += ["-i", stream_url, "-vn", "-c:a", "copy", str(dest)]

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path, *arguments,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    jobs.adopt(process)

    try:
        code = await process.wait()
    except asyncio.CancelledError:
        # A discarded prefetch (the queue was cleared, or stop() ran) - kill rather than let an
        # abandoned ffmpeg keep writing a file nobody will ever read.
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        discard(dest)
        raise

    if code != 0:
        discard(dest)
        raise RuntimeError(f"ffmpeg cache download exited {code}")
