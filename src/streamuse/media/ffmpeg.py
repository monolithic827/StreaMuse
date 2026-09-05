"""Builds and runs the encoder.

Two loopback TCP inputs, not stdin: stdin carries one stream, and keeping stdout free lets
`-progress pipe:1` deliver structured stats while stderr stays for log lines.
"""

import asyncio
import subprocess
from pathlib import Path

from .. import jobs

CREATE_NO_WINDOW = 0x08000000

#: What the encoder emits, regardless of what the receiver delivers.
OUTPUT_SAMPLE_RATE = 48000


def build_arguments(settings, audio_port: int, video_port: int, sample_rate: int,
                    output_directory: Path) -> list[str]:
    fps = str(settings.fps)

    return [
        "-hide_banner", "-nostdin",
        "-loglevel", "level+warning",

        # ffmpeg's default 5 s probe does not drain the audio socket; the paced writer fills the
        # buffer, blocks, and sheds seconds of audio.
        "-analyzeduration", "0",
        "-probesize", "32",
        "-thread_queue_size", "1024",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "2",
        "-i", f"tcp://127.0.0.1:{audio_port}",

        # probesize must still admit one whole JPEG.
        "-analyzeduration", "0",
        "-probesize", "5000000",
        "-thread_queue_size", "256",
        "-f", "image2pipe",
        "-framerate", fps,
        "-i", f"tcp://127.0.0.1:{video_port}",

        "-map", "1:v:0", "-map", "0:a:0",

        # JPEG input is full-range and would otherwise leak out as yuvj420p.
        "-vf", "scale=in_range=full:out_range=tv,format=yuv420p",
        "-color_range", "tv",

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "stillimage",
        "-profile:v", "main",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-r", fps,
        "-g", fps,
        "-keyint_min", fps,
        "-sc_threshold", "0",
        "-b:v", f"{settings.videoBitrateKbps}k",
        "-maxrate", f"{int(settings.videoBitrateKbps * 1.25)}k",
        "-bufsize", f"{settings.videoBitrateKbps * 2}k",

        "-c:a", "aac",
        "-b:a", f"{settings.audioBitrateKbps}k",
        "-ar", str(OUTPUT_SAMPLE_RATE),
        "-ac", "2",

        "-fps_mode", "cfr",

        "-f", "hls",
        "-hls_time", "1",
        "-hls_list_size", "6",
        "-hls_delete_threshold", "2",
        "-hls_segment_type", "mpegts",
        "-hls_flags", "delete_segments+independent_segments+omit_endlist+program_date_time",
        "-hls_segment_filename", str(output_directory / "seg_%06d.ts"),
        str(output_directory / "index.m3u8"),

        "-progress", "pipe:1",
        "-stats_period", "1",
    ]


class FfmpegEncoder:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._process: subprocess.Popen | None = None
        self._readers: list[asyncio.Task] = []
        self.dropped_frames = 0
        self.on_exit = None

    async def start(self, ffmpeg_path: str, settings, audio_port: int, video_port: int,
                    sample_rate: int, output_directory: Path) -> None:
        arguments = build_arguments(settings, audio_port, video_port, sample_rate, output_directory)

        process = await asyncio.create_subprocess_exec(
            ffmpeg_path, *arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        self._process = process
        jobs.adopt(process)

        self._readers = [
            asyncio.create_task(self._read_progress(process.stdout)),
            asyncio.create_task(self._read_log(process.stderr)),
            asyncio.create_task(self._watch_exit(process)),
        ]

    async def _watch_exit(self, process) -> None:
        code = await process.wait()
        if self.on_exit is not None:
            self.on_exit(code)

    async def _read_progress(self, stream) -> None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("drop_frames="):
                continue
            try:
                self.dropped_frames = int(line[len("drop_frames="):])
            except ValueError:
                pass

    async def _read_log(self, stream) -> None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            # Range is handled in the filter chain; this warning is noise on every start.
            if "deprecated pixel format used" in line.lower():
                continue

            lowered = line.lower()
            level = "error" if "[error]" in lowered or "[fatal]" in lowered else "warn"
            self._hub.log(level, f"ffmpeg: {_shorten(line)}")

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return

        self.on_exit = None
        for task in self._readers:
            task.cancel()
        self._readers = []

        try:
            if process.returncode is None:
                process.kill()
        except OSError:
            pass


def _shorten(line: str) -> str:
    return line if len(line) <= 200 else line[:200] + "…"
