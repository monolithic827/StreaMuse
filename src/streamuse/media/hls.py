from .. import paths


def prepare() -> None:
    """Clears the previous run so a reconnecting client cannot be handed stale segments."""
    paths.HLS_DIR.mkdir(parents=True, exist_ok=True)

    for file in paths.HLS_DIR.iterdir():
        if file.suffix.lower() not in (".ts", ".m3u8"):
            continue
        try:
            file.unlink()
        except OSError:
            pass


def measure_bitrate_kbps() -> int:
    """Delivered bitrate over the last few 1 s segments, skipping the newest because ffmpeg may
    still be writing it. Measured from file sizes: the HLS muxer reports bitrate=N/A."""
    try:
        segments = sorted(paths.HLS_DIR.glob("seg_*.ts"), key=lambda f: f.name, reverse=True)[1:5]
        if not segments:
            return 0
        return round(sum(f.stat().st_size for f in segments) * 8 / len(segments) / 1000)
    except OSError:
        return 0


def local_url(public_port: int, stream_key: str) -> str:
    return f"http://127.0.0.1:{public_port}/live/{stream_key}/index.m3u8"
