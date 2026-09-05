"""User-facing configuration. Encoder fields are read only when the stream starts."""

import json
from dataclasses import asdict, dataclass, fields

from . import paths

SOURCES = ("apple", "spotify")
TUNNEL_MODES = ("Quick", "Named")
THEMES = ("Auto", "Dark", "Light")


@dataclass
class Settings:
    source: str = "apple"

    #: Path component of the public playlist: /live/{streamKey}/index.m3u8.
    streamKey: str = "parlour"

    #: The name Apple Music and Spotify show in their device pickers.
    receiverName: str = "StreaMuse"

    width: int = 1280
    height: int = 720
    fps: int = 10
    videoBitrateKbps: int = 400
    audioBitrateKbps: int = 320

    textOverlay: bool = True

    tunnelMode: str = "Quick"
    namedTunnelToken: str = ""
    namedTunnelHostname: str = ""
    autoTunnel: bool = True

    logExpanded: bool = False
    theme: str = "Auto"

    def normalized(self) -> "Settings":
        """Clamps anything a hand-edited file (or a stale schema) could have made invalid."""
        self.source = self.source if self.source in SOURCES else "apple"
        self.streamKey = _sanitize_key(self.streamKey)
        self.receiverName = (self.receiverName or "").strip()[:63] or "StreaMuse"
        self.width = _clamp(_even_up(self.width), 256, 3840)
        self.height = _clamp(_even_up(self.height), 256, 2160)
        self.fps = _clamp(self.fps, 1, 30)
        self.videoBitrateKbps = _clamp(self.videoBitrateKbps, 100, 9000)
        self.audioBitrateKbps = _clamp(self.audioBitrateKbps, 64, 512)
        self.tunnelMode = self.tunnelMode if self.tunnelMode in TUNNEL_MODES else "Quick"
        self.theme = self.theme if self.theme in THEMES else "Auto"
        return self

    def apply(self, other: "Settings") -> None:
        """Copies onto the live instance, which every snapshot already embeds."""
        for field in fields(self):
            setattr(self, field.name, getattr(other, field.name))

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self) -> None:
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = paths.SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(paths.SETTINGS_PATH)


def load() -> Settings:
    try:
        raw = json.loads(paths.SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt or missing file must never stop startup.
        return Settings()

    return from_dict(raw)


def from_dict(raw: dict) -> Settings:
    """Tolerates the PascalCase the C# build wrote, so an existing file keeps its tunnel token."""
    if not isinstance(raw, dict):
        return Settings()

    lowered = {str(k).lower(): v for k, v in raw.items()}
    values = {}

    for field in fields(Settings):
        value = lowered.get(field.name.lower())
        if value is None:
            continue
        try:
            values[field.name] = field.type(value) if field.type is not str else str(value)
        except (TypeError, ValueError):
            continue

    # "External" was a capture target, not a receiver; it has no equivalent now.
    if "source" in values:
        values["source"] = values["source"].lower()

    return Settings(**values).normalized()


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _even_up(value: int) -> int:
    return value if value % 2 == 0 else value + 1


def _sanitize_key(key: str) -> str:
    cleaned = "".join(c for c in (key or "") if c.isascii() and (c.isalnum() or c in "-_"))
    return cleaned[:64] if cleaned else "live"
