"""Draws the video track as JPEG frames pushed down a socket, so the picture can change mid-stream
without restarting ffmpeg and breaking the playlist."""

import functools
import io
import os
from pathlib import Path

import arabic_reshaper
from bidi import get_display
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

JPEG_QUALITY = 88

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

_FALLBACK_TITLE = ["YuGothB.ttc", "ebrima.ttf", "gadugi.ttf", "NirmalaB.ttf", "leelawui.ttf",
                   "seguisym.ttf", "himalaya.ttf", "msyi.ttf", "phagspa.ttf"]
_FALLBACK_BODY = ["YuGothR.ttc", "ebrima.ttf", "gadugi.ttf", "Nirmala.ttf", "leelawui.ttf",
                  "seguisym.ttf", "himalaya.ttf", "msyi.ttf", "phagspa.ttf"]


class CoverFrameRenderer:
    def __init__(self, settings, artwork, hub) -> None:
        self._artwork = artwork
        self._hub = hub

        self._width = settings.width
        self._height = settings.height
        self._overlay = settings.textOverlay

        self._title_font_path = _FONTS / "seguisb.ttf"
        self._body_font_path = _FONTS / "segoeui.ttf"

        self._title_font = ImageFont.truetype(self._title_font_path, self._height * 0.075)
        self._body_font = ImageFont.truetype(self._body_font_path, self._height * 0.045)
        self._small_font = ImageFont.truetype(self._body_font_path, self._height * 0.033)

        self._title_fallbacks = _load_fonts(_FALLBACK_TITLE, self._height * 0.075)
        self._body_fallbacks = _load_fonts(_FALLBACK_BODY, self._height * 0.045)
        self._small_fallbacks = _load_fonts(_FALLBACK_BODY, self._height * 0.033)

        self._art_box = _square_in(self._width, self._height,
                                   0.62 if self._overlay else 0.86, align_left=self._overlay)

        self._decoded: Image.Image | None = None
        self._decoded_version = -1
        self._background = DEFAULT_BACKGROUND
        #: The blur dominates the frame cost and only changes with the cover, so the composed
        #: ground is kept while the text above it is redrawn.
        self._ground: Image.Image | None = None

        self._last_frame: bytes | None = None
        self._last_signature = ""

    def render(self) -> bytes:
        """Current frame as JPEG, re-rendered only when something visible changed."""
        now = self._hub.now_playing
        progress = (
            min(1.0, max(0.0, now.positionSeconds / now.durationSeconds))
            if now.durationSeconds > 0 else 0.0
        )

        # Quantised so a static track does not force a re-encode every frame.
        signature = "|".join(str(part) for part in (
            self._artwork.version, now.title, now.artist, now.album, int(progress * 600)))

        if self._last_frame is not None and signature == self._last_signature:
            return self._last_frame

        self._last_frame = self._render_frame(now, progress)
        self._last_signature = signature
        return self._last_frame

    def _render_frame(self, now, progress: float) -> bytes:
        frame = self._ensure_ground().copy()

        if self._overlay:
            self._draw_text(frame, now, progress)

        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
        return buffer.getvalue()

    def _ensure_ground(self) -> Image.Image:
        self._ensure_artwork_decoded()
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

    def _draw_text(self, frame: Image.Image, now, progress: float) -> None:
        width, height = self._width, self._height
        left = self._art_box[2] + width * 0.045
        right = width - width * 0.05
        available = right - left
        if available < 80:
            return

        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        y = self._art_box[1] + height * 0.10

        title = now.title or "Nothing playing"
        _draw_with_fallback(draw, (left, y), _rtl(title, self._title_font, available),
                            self._title_font, self._title_font_path, self._title_fallbacks, BRIGHT)
        y += height * 0.085

        _draw_with_fallback(draw, (left, y), _rtl(now.artist, self._body_font, available),
                            self._body_font, self._body_font_path, self._body_fallbacks, MUTED)
        y += height * 0.06

        _draw_with_fallback(draw, (left, y), _rtl(now.album, self._small_font, available),
                            self._small_font, self._body_font_path, self._small_fallbacks, FAINT)

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

    def _ensure_artwork_decoded(self) -> None:
        version = self._artwork.version
        if version == self._decoded_version:
            return

        self._decoded_version = version
        self._decoded = None
        self._ground = None
        self._background = DEFAULT_BACKGROUND

        data = self._artwork.bytes
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


def _rtl(text: str, font, max_width: float) -> str:
    #: Reshape and ellipsize before reordering into visual order - see CLAUDE.md.
    return get_display(_ellipsize(arabic_reshaper.reshape(text), font, max_width))


@functools.lru_cache(maxsize=None)
def _cmap(path: Path) -> frozenset[int]:
    kwargs = {"fontNumber": 0} if path.suffix.lower() == ".ttc" else {}
    with TTFont(path, lazy=True, **kwargs) as face:
        return frozenset(face.getBestCmap())


def _load_fonts(names: list[str], size: float) -> list[tuple[ImageFont.FreeTypeFont, Path]]:
    return [(ImageFont.truetype(_FONTS / name, size), _FONTS / name) for name in names]


def _draw_with_fallback(draw, xy, text: str, font, font_path: Path,
                         fallbacks: list[tuple[ImageFont.FreeTypeFont, Path]], fill) -> None:
    """A run of characters `font` can't cover is drawn with the first font in `fallbacks` that
    actually has the glyph, instead of the .notdef tofu box PIL would otherwise draw silently."""
    x, y = xy
    run, run_font = "", font

    def flush():
        if run:
            draw.text((x, y), run, font=run_font, fill=fill, anchor="ls")

    for ch in text:
        chosen = font
        if ord(ch) not in _cmap(font_path):
            for fallback_font, fallback_path in fallbacks:
                if ord(ch) in _cmap(fallback_path):
                    chosen = fallback_font
                    break
        if chosen is not run_font:
            flush()
            x += run_font.getlength(run)
            run = ""
        run += ch
        run_font = chosen
    flush()


def _format_clock(seconds: float) -> str:
    if seconds != seconds or seconds < 0:
        seconds = 0
    total = int(seconds)
    hours, minutes, secs = total // 3600, total // 60 % 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
