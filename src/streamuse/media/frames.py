"""Draws the video track as JPEG frames pushed down a socket, so the picture can change mid-stream
without restarting ffmpeg and breaking the playlist."""

import io
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..state import NowPlaying

JPEG_QUALITY = 88

REQUESTED_PILL = (0x94, 0xBC, 0xE3, 0xFF)
REQUESTED_TEXT = (0x10, 0x12, 0x14, 0xFF)

DEFAULT_BACKGROUND = (0x1D, 0x1F, 0x20)
PLACEHOLDER_FILL = (0x2B, 0x2B, 0x2D)
PLACEHOLDER_STROKE = (0x59, 0x80, 0xA6)
SHADE = (0x10, 0x12, 0x14, 0xC0)

BRIGHT = (0xFF, 0xFF, 0xFF, 0xFF)
MUTED = (0xFF, 0xFF, 0xFF, 0xB0)
FAINT = (0xFF, 0xFF, 0xFF, 0x70)
ACCENT = (0x94, 0xBC, 0xE3, 0xFF)
TRACK = (0xFF, 0xFF, 0xFF, 0x33)

_FONTS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"


class CoverFrameRenderer:
    def __init__(self, settings, artwork, hub, dj=None) -> None:
        self._artwork = artwork
        self._hub = hub
        self._dj = dj

        self._width = settings.width
        self._height = settings.height
        self._overlay = settings.textOverlay

        self._title_font = ImageFont.truetype(_FONTS / "seguisb.ttf", self._height * 0.075)
        self._body_font = ImageFont.truetype(_FONTS / "segoeui.ttf", self._height * 0.045)
        self._small_font = ImageFont.truetype(_FONTS / "segoeui.ttf", self._height * 0.033)

        self._art_box = _square_in(self._width, self._height,
                                   0.62 if self._overlay else 0.86, align_left=self._overlay)

        self._decoded: Image.Image | None = None
        self._decoded_version = -1
        self._decoded_source = None
        self._background = DEFAULT_BACKGROUND
        #: The blur dominates the frame cost and only changes with the cover, so the composed
        #: ground is kept while the text above it is redrawn.
        self._ground: Image.Image | None = None

        self._last_frame: bytes | None = None
        self._last_signature = ""

    def render(self) -> bytes:
        """Current frame as JPEG, re-rendered only when something visible changed."""
        dj_state = self._dj.snapshot() if self._dj is not None else None
        playing = dj_state.nowMixing if dj_state is not None else None
        mixing = playing is not None

        if mixing:
            now = NowPlaying(playing.title, playing.artist, dj_state.album, True,
                             dj_state.positionSeconds, dj_state.durationSeconds,
                             dj_state.artworkVersion)
            artwork = self._dj.artwork
        else:
            now = self._hub.now_playing
            artwork = self._artwork

        progress = (
            min(1.0, max(0.0, now.positionSeconds / now.durationSeconds))
            if now.durationSeconds > 0 else 0.0
        )

        # Quantised so a static track does not force a re-encode every frame.
        signature = "|".join(str(part) for part in (
            mixing, artwork.version, now.title, now.artist, now.album, int(progress * 600)))

        if self._last_frame is not None and signature == self._last_signature:
            return self._last_frame

        self._last_frame = self._render_frame(now, progress, artwork, mixing)
        self._last_signature = signature
        return self._last_frame

    def _render_frame(self, now, progress: float, artwork, mixing: bool) -> bytes:
        frame = self._ensure_ground(artwork).copy()

        if self._overlay:
            self._draw_text(frame, now, progress, mixing)

        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
        return buffer.getvalue()

    def _ensure_ground(self, artwork) -> Image.Image:
        self._ensure_artwork_decoded(artwork)
        if self._ground is not None:
            return self._ground

        ground = Image.new("RGB", (self._width, self._height), self._background)
        art = self._decoded

        if art is not None:
            self._draw_backdrop(ground, art)
            ground.paste(_fit(art, self._art_box), _fit_origin(art, self._art_box))
        else:
            draw = ImageDraw.Draw(ground)
            draw.rectangle(self._art_box, fill=PLACEHOLDER_FILL, outline=PLACEHOLDER_STROKE, width=2)

        self._ground = ground
        return ground

    def _draw_backdrop(self, ground: Image.Image, art: Image.Image) -> None:
        """A blurred, darkened blow-up of the art so the frame is never flat black."""
        width, height = self._width, self._height
        scale = max(width / art.width, height / art.height) * 1.4
        size = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))

        blown = art.resize(size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(40))
        ground.paste(blown, ((width - size[0]) // 2, (height - size[1]) // 2))

        shade = Image.new("RGBA", (width, height), SHADE)
        ground.paste(shade, (0, 0), shade)

    def _draw_text(self, frame: Image.Image, now, progress: float, mixing: bool = False) -> None:
        width, height = self._width, self._height
        left = self._art_box[2] + width * 0.045
        right = width - width * 0.05
        available = right - left
        if available < 80:
            return

        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        y = self._art_box[1] + height * 0.10

        if mixing:
            # Dropped-in audio otherwise looks identical to normal playback in the frame.
            label = "REQUESTED"
            pad_x, pad_y = width * 0.012, height * 0.006
            pill_height = self._small_font.size + pad_y * 2
            pill_top = y - pill_height - height * 0.02
            pill_width = self._small_font.getlength(label) + pad_x * 2
            draw.rounded_rectangle(
                (left, pill_top, left + pill_width, pill_top + pill_height),
                radius=pill_height * 0.3, fill=REQUESTED_PILL)
            draw.text((left + pad_x, pill_top + pad_y), label,
                      font=self._small_font, fill=REQUESTED_TEXT, anchor="la")

        title = now.title or "Nothing playing"
        draw.text((left, y), _ellipsize(title, self._title_font, available),
                  font=self._title_font, fill=BRIGHT, anchor="ls")
        y += height * 0.085

        draw.text((left, y), _ellipsize(now.artist, self._body_font, available),
                  font=self._body_font, fill=MUTED, anchor="ls")
        y += height * 0.06

        draw.text((left, y), _ellipsize(now.album, self._small_font, available),
                  font=self._small_font, fill=FAINT, anchor="ls")

        bar_y = self._art_box[3] - height * 0.06
        bar_height = max(2.0, height * 0.006)

        draw.rectangle((left, bar_y, right, bar_y + bar_height), fill=TRACK)
        if progress > 0:
            draw.rectangle((left, bar_y, left + available * progress, bar_y + bar_height),
                           fill=ACCENT)

        elapsed = _format_clock(now.positionSeconds)
        total = _format_clock(now.durationSeconds) if now.durationSeconds > 0 else "--:--"
        times_y = bar_y + height * 0.045

        draw.text((left, times_y), elapsed, font=self._small_font, fill=FAINT, anchor="ls")
        draw.text((right - self._small_font.getlength(total), times_y), total,
                  font=self._small_font, fill=FAINT, anchor="ls")

        frame.paste(layer, (0, 0), layer)

    def _ensure_artwork_decoded(self, artwork) -> None:
        version = artwork.version
        if version == self._decoded_version and artwork is self._decoded_source:
            return

        self._decoded_version = version
        self._decoded_source = artwork
        self._decoded = None
        self._ground = None
        self._background = DEFAULT_BACKGROUND

        data = artwork.bytes
        if not data:
            return

        try:
            image = Image.open(io.BytesIO(data))
            image.load()
            self._decoded = image.convert("RGB")
        except Exception as exc:
            self._hub.warn(f"could not decode album art: {exc}")
            return

        self._background = _dominant_color(self._decoded)


def _dominant_color(image: Image.Image) -> tuple[int, int, int]:
    """Average of a downsampled copy, darkened - good enough as a backdrop tint."""
    pixels = list(image.resize((16, 16), Image.Resampling.BILINEAR).getdata())
    count = len(pixels)
    return tuple(int(sum(p[i] for p in pixels) / count * 0.35) for i in range(3))


def _square_in(width: int, height: int, fraction: float, align_left: bool):
    side = min(width, height) * fraction
    top = height / 2 - side / 2
    left = height * 0.09 if align_left else width / 2 - side / 2
    return (left, top, left + side, top + side)


def _fit(art: Image.Image, box) -> Image.Image:
    scale = min((box[2] - box[0]) / art.width, (box[3] - box[1]) / art.height)
    return art.resize((max(1, round(art.width * scale)), max(1, round(art.height * scale))),
                      Image.Resampling.BILINEAR)


def _fit_origin(art: Image.Image, box) -> tuple[int, int]:
    scale = min((box[2] - box[0]) / art.width, (box[3] - box[1]) / art.height)
    return (round((box[0] + box[2]) / 2 - art.width * scale / 2),
            round((box[1] + box[3]) / 2 - art.height * scale / 2))


def _ellipsize(text: str, font, max_width: float) -> str:
    if not text or font.getlength(text) <= max_width:
        return text

    trimmed = text
    while len(trimmed) > 1 and font.getlength(trimmed + "\u2026") > max_width:
        trimmed = trimmed[:-1]

    return trimmed + "\u2026"


def _format_clock(seconds: float) -> str:
    if seconds != seconds or seconds < 0:
        seconds = 0
    total = int(seconds)
    hours, minutes, secs = total // 3600, total // 60 % 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
