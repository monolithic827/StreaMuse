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


async def _drain(stream: asyncio.StreamReader) -> bytearray:
    buffer = bytearray()
    while chunk := await stream.read(1 << 16):
        buffer += chunk
    return buffer


async def download(ffmpeg_path: str, stream_url: str, http_headers: dict[str, str]) -> bytearray:
    arguments = [
        "-hide_banner", "-nostdin", "-loglevel", "level+warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
    ]
    if http_headers:
        arguments += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())]
    arguments += ["-i", stream_url, "-vn", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1"]

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path, *arguments,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    jobs.adopt(process)

    try:
        # Both drained while the process runs, which a read after wait() cannot do - the pipe's OS
        # buffer is far smaller than a track, so ffmpeg would block writing long before it exits.
        # Into a bytearray rather than communicate()'s list of chunks joined at the end, which would
        # briefly hold a whole track's PCM twice.
        pcm, log = await asyncio.gather(_drain(process.stdout), _drain(process.stderr))
        await process.wait()
    except asyncio.CancelledError:
        # A discarded prefetch (the queue was cleared, or stop() ran) - kill rather than let an
        # abandoned ffmpeg keep decoding into a pipe nobody will ever read.
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise

    if process.returncode != 0:
        # The last line is what actually says why - a CDN 403 from a stale signed URL looks nothing
        # like a codec failure, and the exit code alone cannot tell them apart.
        detail = log.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg exited {process.returncode}: {detail[-1] if detail else 'no output'}")
    return pcm
