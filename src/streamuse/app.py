"""Wiring and lifetime.

pywebview must own the main thread on Windows, so everything else runs on one asyncio loop on a
background thread and the window blocks until the user closes it.
"""

import argparse
import asyncio
import socket
import sys
import threading

from . import paths, settings as settings_module, ui
from .artwork import ArtworkStore
from .deps import DependencyManager
from .dj.mixer import DjMixer
from .media import hls
from .media.pipeline import StreamPipeline
from .sources import SAMPLE_RATE, SourceManager
from .sources.airplay.receiver import AirPlayReceiver
from .sources.spotify.receiver import SpotifyReceiver
from .state import StateHub
from .tunnel import CloudflaredTunnel
from .web import control, public
from aiohttp import web

CONTROL_PORT = 7788
PUBLIC_PORT = 7789


class Runtime:
    """The asyncio loop, on its own thread, plus the ordered shutdown."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="streamuse", daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()

    def call(self, coroutine, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def spawn(self, coroutine) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


def build(hub, settings, control_port: int, public_port: int):
    artwork = ArtworkStore()
    deps = DependencyManager(hub)
    tunnel = CloudflaredTunnel(settings, hub, deps, public_port)

    # dj needs sources (to pause/resume it), sources needs pipeline.push_audio (as its sink), and
    # pipeline needs dj (to hand to the pacer) - pipeline.set_dj breaks that three-way cycle.
    pipeline = StreamPipeline(settings, hub, deps, artwork, tunnel, SAMPLE_RATE)

    sources = SourceManager(settings, hub, artwork, pipeline.push_audio, {
        "apple": AirPlayReceiver(settings, hub, artwork),
        "spotify": SpotifyReceiver(settings, hub, artwork, deps),
    })
    dj = DjMixer(settings, hub, deps, sources, SAMPLE_RATE)
    pipeline.set_dj(dj)

    control_app = control.build_app(
        hub, deps, artwork, settings, pipeline, tunnel, sources, dj, public_port)
    public_app = public.build_app(hub, artwork, settings, dj)

    return artwork, deps, tunnel, pipeline, sources, dj, control_app, public_app


async def serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def pick_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    _use_utf8()
    parser = argparse.ArgumentParser(prog="streamuse")
    parser.add_argument("--test-receiver", choices=("apple", "spotify"),
                        help="run one receiver alone and report what it delivers")
    parser.add_argument("seconds", nargs="?", type=int, default=30)
    arguments = parser.parse_args()

    if arguments.test_receiver:
        _attach_console()
        from .selftest import run_receiver_test
        asyncio.run(run_receiver_test(arguments.test_receiver, arguments.seconds))
        return

    paths.ensure()
    settings = settings_module.load()
    hub = StateHub(settings)

    control_port = pick_port(CONTROL_PORT)
    public_port = pick_port(PUBLIC_PORT)

    runtime = Runtime()
    hub.bind_loop(runtime.loop)
    runtime.start()

    artwork, deps, tunnel, pipeline, sources, dj, control_app, public_app = build(
        hub, settings, control_port, public_port)

    hub.set_local_url(hls.local_url(public_port, settings.streamKey))

    runners = [
        runtime.call(serve(control_app, control_port)),
        runtime.call(serve(public_app, public_port)),
    ]
    hub.info(f"control panel on 127.0.0.1:{control_port}, HLS on 127.0.0.1:{public_port}")

    runtime.spawn(_prepare(hub, deps, sources, settings, dj))

    ui.run(f"http://127.0.0.1:{control_port}/", f"http://127.0.0.1:{control_port}/dj", settings)

    _shutdown(runtime, hub, pipeline, sources, tunnel, runners)


async def _prepare(hub, deps, sources, settings, dj) -> None:
    # The receiver needs none of the downloads, and behind ~135 MB of them a first launch offers
    # Apple Music no speaker to pick for minutes. ffmpeg and cloudflared are wanted later, by the
    # stream and tunnel buttons, and both say so themselves when they are missing.
    sources.start_publishing()
    dj.start()
    await sources.select(settings.source)

    try:
        await deps.ensure_all()
    except Exception as exc:
        hub.error(f"dependency check failed: {exc}")

def _shutdown(runtime, hub, pipeline, sources, tunnel, runners) -> None:
    """Runs with the window already gone and nothing above it to catch anything. Every step is
    attempted regardless of the ones before it: leaving ffmpeg or cloudflared running would keep the
    stream publicly live."""
    for what, coroutine in (
        ("stop the stream", pipeline.stop()),
        ("stop the receiver", sources.stop()),
        ("stop the tunnel", tunnel.stop()),
        *(("stop the web host", runner.cleanup()) for runner in runners),
    ):
        try:
            runtime.call(coroutine, timeout=10)
        except Exception as exc:
            _report(hub, f"shutdown: could not {what} - {exc}")

    try:
        runtime.shutdown()
    except Exception as exc:
        _report(hub, f"shutdown: could not stop the loop - {exc}")


def _report(hub, message: str) -> None:
    try:
        hub.error(message)
    except Exception:
        pass


def _attach_console() -> None:
    """The exe is windowed, so the diagnostic modes have to borrow the console that launched
    them - without this their output goes nowhere."""
    import ctypes

    try:
        if sys.stdout is not None and sys.stdout.fileno() >= 0:
            return
    except (OSError, ValueError):
        pass

    if not ctypes.windll.kernel32.AttachConsole(-1):
        ctypes.windll.kernel32.AllocConsole()

    for name in ("stdout", "stderr"):
        setattr(sys, name, open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1))


def _use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
