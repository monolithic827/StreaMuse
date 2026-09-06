"""`--test-receiver`: runs one receiver alone and reports what it actually delivered.

This is the fastest answer to "why is the stream silent" - it says whether a sender ever connected,
what it reported, how much audio arrived, and how far that audio drifted from wall clock.
"""

import array
import asyncio
import math
import time
import wave

from . import paths, settings as settings_module
from .artwork import ArtworkStore, content_type_of
from .deps import DependencyManager
from .sources import SAMPLE_RATE
from .state import StateHub


async def run_receiver_test(source: str, seconds: int) -> None:
    paths.ensure()
    settings = settings_module.load()
    hub = StateHub(settings)
    hub.bind_loop(asyncio.get_running_loop())
    artwork = ArtworkStore()

    deps = DependencyManager(hub)
    receiver = _build(source, settings, hub, artwork, deps)
    if not receiver.available:
        print(f"\n{source} is unavailable: {receiver.reason}")
        return

    captured = bytearray()
    await receiver.start(captured.extend)

    print(f"\nListening for {seconds}s. {receiver.status_text}\n")
    started = time.monotonic()

    try:
        for remaining in range(seconds, 0, -1):
            await asyncio.sleep(1)
            track = receiver.track()
            print(f"  {remaining:3d}s left | connected={receiver.connected} "
                  f"| {len(captured) // 4:>8} frames | {track.title[:40] or '-'}")
    finally:
        elapsed = time.monotonic() - started
        # Read the fields out before stopping: the receiver clears the track it hands back.
        track = receiver.track().snapshot(artwork.version)
        client = receiver.client
        cover = artwork.bytes
        await receiver.stop()

    _report(captured, elapsed, track, client, cover)


def _build(source: str, settings, hub, artwork, deps):
    if source == "apple":
        from .sources.airplay.receiver import AirPlayReceiver
        return AirPlayReceiver(settings, hub, artwork)

    if source == "device":
        from .sources.device.receiver import DeviceReceiver
        return DeviceReceiver(settings, hub)

    from .sources.spotify.receiver import SpotifyReceiver
    return SpotifyReceiver(settings, hub, artwork, deps)


def _report(captured: bytearray, elapsed: float, track, client: str, cover) -> None:
    frames = len(captured) // 4
    print("\n" + "-" * 64)
    print(f"client          {client or 'nothing connected'}")
    print(f"track           {track.title or '-'} / {track.artist or '-'} / {track.album or '-'}")
    print(f"duration        {track.durationSeconds:.1f}s, position {track.positionSeconds:.1f}s")

    print(f"artwork         {len(cover)} bytes, {content_type_of(cover)}" if cover else
          "artwork         none received")

    if not frames:
        print("audio           nothing received")
        print("-" * 64)
        return

    samples = array.array("h")
    samples.frombytes(bytes(captured))
    peak = max(max(samples), -min(samples)) / 32768
    silent = sum(1 for i in range(0, len(samples), 441 * 2) if not any(samples[i:i + 882]))
    windows = max(1, len(samples) // (441 * 2))

    print(f"audio           {frames} frames = {frames / SAMPLE_RATE:.2f}s in {elapsed:.2f}s wall")
    print(f"drift           {frames / SAMPLE_RATE - elapsed:+.2f}s")
    print(f"peak            {20 * math.log10(peak):.1f} dBFS" if peak else "peak            silent")
    print(f"silence         {silent * 100 // windows}% of 10 ms windows")

    output = paths.DATA_DIR / "test-receiver.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(captured))
    print(f"wrote           {output}")
    print("-" * 64)
