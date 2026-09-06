"""Builds candidate tracks for the DJ library with no official playlist API and no OAuth - either by
walking forward through whatever's already playing on the live source (skip, read the metadata that
arrives, repeat) or by reading a YouTube Music playlist URL directly, the same --flat-playlist
technique fetch.py's search() already uses.

Harvesting the live source is real playback: each "next" actually advances Apple Music/Spotify, which
push_audio would relay to the public stream if one were running. media/pipeline.py's push_audio
already drops everything while no stream session exists, so this is safe with no new gating code as
long as it's run before going live - the DJ window's "Learn" button says so rather than this module
enforcing it, matching this codebase's preference for trusting the user over hard-blocking.
"""

import asyncio
import subprocess

from .. import jobs
from . import fetch as fetch_module

SKIP_POLL_INTERVAL = 0.3

#: How long a skip may produce no metadata change before the queue is taken to have ended - neither
#: DACP nor go-librespot says so directly, both only ack the command.
SKIP_TIMEOUT = 5.0

MAX_LIVE_TRACKS = 200
MAX_PLAYLIST_TRACKS = 500


async def harvest_live_source(sources, hub, fetcher) -> list[tuple[str, str, str]]:
    """Returns (video_id, title, artist) triples. Neither AirPlay's DACP nor go-librespot hands over a
    video_id, so each harvested (title, artist) pair is resolved through the same YouTube Music search
    a manual request already uses - taking the first hit, same as fetch() does when nothing more
    specific was picked."""
    receiver = sources.active
    if receiver is None:
        raise RuntimeError("no source is selected")

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    started = _now_playing(receiver)
    if started != ("", ""):
        seen.add(started)
        pairs.append(started)

    while len(pairs) < MAX_LIVE_TRACKS:
        previous = _now_playing(receiver)
        if not await sources.control("next"):
            hub.warn("dj library: 'next' was refused - stopping the harvest here")
            break

        current = await _wait_for_change(receiver, previous)
        if current is None:
            break  # no change within the timeout - end of queue, or a repeat-one loop
        if current == started or current in seen:
            break  # back to where we started, or a track already seen - the playlist has looped
        seen.add(current)
        pairs.append(current)

    hub.info(f"dj library: harvested {len(pairs)} tracks from the live source - resolving each one")

    resolved: list[tuple[str, str, str]] = []
    for title, artist in pairs:
        query = f"{artist} {title}".strip() if artist else title
        try:
            results = await fetcher.search(query)
        except Exception as exc:
            hub.warn(f"dj library: could not resolve \"{query}\": {exc}")
            continue
        if results:
            resolved.append((results[0].video_id, title, artist))

    return resolved


def _now_playing(receiver) -> tuple[str, str]:
    track = receiver.track()
    return track.title, track.artist


async def _wait_for_change(receiver, previous: tuple[str, str]) -> tuple[str, str] | None:
    elapsed = 0.0
    while elapsed < SKIP_TIMEOUT:
        await asyncio.sleep(SKIP_POLL_INTERVAL)
        elapsed += SKIP_POLL_INTERVAL
        current = _now_playing(receiver)
        if current != previous and current != ("", ""):
            return current
    return None


async def harvest_youtube_playlist(deps, hub, url: str) -> list[tuple[str, str, str]]:
    """Returns (video_id, title, artist) triples directly - a playlist URL names every track's own
    video, so unlike the live-source harvest there's no search-resolution step needed."""
    if deps.yt_dlp is None:
        raise RuntimeError("yt-dlp is not available - check the Dependencies panel")

    process = await asyncio.create_subprocess_exec(
        deps.yt_dlp,
        "--flat-playlist", "--quiet", "--no-warnings",
        "--playlist-items", f"1-{MAX_PLAYLIST_TRACKS}",
        "--print", fetch_module.SEPARATOR.join(
            ("%(id)s", "%(track,title)s", "%(artist,uploader,channel)s")),
        url,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=fetch_module.CREATE_NO_WINDOW,
    )
    jobs.adopt(process)

    try:
        out, err = await asyncio.wait_for(process.communicate(), 120)
    except asyncio.TimeoutError:
        process.kill()
        raise RuntimeError("reading that playlist timed out")

    if process.returncode != 0:
        raise RuntimeError(f"could not read that playlist: {_tail(err)}")

    results = []
    for line in out.decode("utf-8", "replace").splitlines():
        fields = line.split(fetch_module.SEPARATOR)
        if len(fields) != 3:
            continue
        video_id, title, artist = (f.strip() for f in fields)
        if not video_id or video_id == "NA":
            continue
        results.append((video_id, title if title and title != "NA" else "Unknown title",
                        "" if artist == "NA" else artist))

    hub.info(f"dj library: read {len(results)} tracks from the playlist")
    return results


def _tail(stream: bytes, limit: int = 300) -> str:
    text = stream.decode("utf-8", "replace").strip()
    return text if len(text) <= limit else text[-limit:]
