"""Resolves and decodes a track into memory before it plays - network fetch, resample and PCM decode
all in one ffmpeg pass, off the critical path during prefetch.

A CDN reset during this step only costs download time - ffmpeg's own `-reconnect` keeps retrying
against a target with no realtime deadline to miss, unlike the same reset landing mid-playback
against a paced decoder (see decoder.py and CLAUDE.md's "audio buffer overran" note). Doing the full
decode here rather than deferring it to playback time means nothing during playback ever depends on
a live ffmpeg process again - decoder.py's job shrinks to pacing bytes already sitting in memory.
Never touching disk for the downloaded audio also means nothing here is a file antivirus real-time
scanning can intercept mid-open - a track's cache is just a `bytes` object, dropped like any other
reference once it stops being needed rather than explicitly cleaned up.
"""

import asyncio
import contextlib
import subprocess

from ... import jobs
from .. import SAMPLE_RATE

CREATE_NO_WINDOW = 0x08000000


async def download(ffmpeg_path: str, stream_url: str, http_headers: dict[str, str]) -> bytes:
    arguments = [
        "-hide_banner", "-nostdin", "-loglevel", "level+warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
    ]
    if http_headers:
        arguments += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())]
    arguments += ["-i", stream_url, "-vn", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1"]

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path, *arguments,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    jobs.adopt(process)

    try:
        # communicate() rather than a bare wait() + read: it drains stdout concurrently with the
        # process running, which a plain read after wait() cannot do once the pipe's own OS buffer
        # is smaller than a full track - ffmpeg would block writing long before it ever exits.
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        # A discarded prefetch (the queue was cleared, or stop() ran) - kill rather than let an
        # abandoned ffmpeg keep decoding into a pipe nobody will ever read.
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise

    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg cache download exited {process.returncode}")
    return stdout
